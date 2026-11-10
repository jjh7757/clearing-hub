"""예치금 입금·인출 신청 (SPEC 10.9). 은행이 신청하고 허브 관리자가 승인·거절한다.

은행은 잔액을 직접 바꾸지 못한다 (SPEC 10.8). 신청은 "이만큼 넣었다/빼 달라"는 기록일 뿐이고, 잔액은 관리자가
승인할 때 기존 경로(`service.deposit` / `service.withdraw`)로만 바뀐다. 승인과 분개는 같은 트랜잭션이다.

인출할 수 있는 금액 = 잔액 − max(미정산 순채무, 0). 은행 신청으로는 순채무한도(빌린 돈)까지 빼 가지 못한다.
관리자가 한도까지 빼 주려면 기존 관리자 인출(`POST /admin/reserve/{bank_code}/withdraw`)을 쓴다.
미정산 순채무는 transfers 쪽이 계산하므로 이 모듈은 계산 함수(`exposure_of`)를 받아 쓴다 (service와 같은 경계).

커밋은 하지 않는다. 라우터가 트랜잭션을 정한다.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core import alerts
from clearing_hub.core.exceptions import ApiException, BadRequest, NotFound
from clearing_hub.domains.reserve import service
from clearing_hub.domains.reserve.models import (
    ReserveLedgerEntry, ReserveRequest, ReserveRequestKind, ReserveRequestStatus,
)

ExposureOf = Callable[[str], Awaitable[int]]

MAX_PENDING = 5  # 은행당 결정 대기 신청 수 상한
BANK_LIST_DAYS = 30
BANK_LIST_LIMIT = 100
BANK = "bank"


def _now() -> datetime:
    return datetime.now(UTC)


def _not_found(request_id: int) -> ApiException:
    return NotFound("RESERVE_REQUEST_NOT_FOUND", f"신청 {request_id}을(를) 찾을 수 없습니다")


def _not_pending(req: ReserveRequest) -> ApiException:
    return ApiException(409, "RESERVE_REQUEST_NOT_PENDING",
                        f"신청 {req.request_id}은(는) 이미 {req.status} 상태입니다",
                        extra={"request_id": req.request_id, "status": req.status})


def _insufficient(withdrawable: int, amount: int) -> ApiException:
    return BadRequest("INSUFFICIENT_RESERVE",
                      f"인출할 수 있는 금액({max(withdrawable, 0)}원)보다 많이 신청했습니다 ({amount}원)")


async def withdrawable(db: AsyncSession, bank_code: str, exposure: int) -> int:
    """잔액 − max(미정산 순채무, 0). 음수일 수 있다."""
    account = await service.get_or_create(db, bank_code)
    return account.balance - max(exposure, 0)


async def _pending_withdraw_total(db: AsyncSession, bank_code: str) -> int:
    return int(await db.scalar(
        select(func.coalesce(func.sum(ReserveRequest.amount), 0)).where(
            ReserveRequest.bank_code == bank_code, ReserveRequest.kind == ReserveRequestKind.WITHDRAW,
            ReserveRequest.status == ReserveRequestStatus.PENDING)))


async def _by_ref(db: AsyncSession, bank_code: str, request_ref: str) -> ReserveRequest | None:
    return await db.scalar(select(ReserveRequest).where(ReserveRequest.bank_code == bank_code,
                                                        ReserveRequest.request_ref == request_ref))


def _same(req: ReserveRequest, kind: str, amount: int, memo: str) -> bool:
    return (req.kind, req.amount, req.memo) == (kind, amount, memo)


def _replay(existing: ReserveRequest, kind: str, amount: int, memo: str) -> ReserveRequest:
    if not _same(existing, kind, amount, memo):
        raise ApiException(409, "REQUEST_REF_CONFLICT",
                           f"request_ref {existing.request_ref}은(는) 내용이 다른 신청에 이미 쓰였습니다",
                           extra={"request_id": existing.request_id})
    return existing


async def create(db: AsyncSession, bank_code: str, request_ref: str, kind: str, amount: int, memo: str,
                 exposure_of: ExposureOf) -> tuple[ReserveRequest, bool]:
    """신청을 남긴다. (신청, 새로 만들었는지). 같은 request_ref를 같은 내용으로 다시 보내면 기존 신청을 돌려준다.

    인출 신청은 지금 인출할 수 있는 금액 − 대기 중인 인출 신청 합보다 크면 400 INSUFFICIENT_RESERVE (미리 알려 주는
    검사다. 승인 때 다시 잰다). 대기 신청이 MAX_PENDING개면 409 TOO_MANY_PENDING_REQUESTS."""
    existing = await _by_ref(db, bank_code, request_ref)
    if existing is not None:
        return _replay(existing, kind, amount, memo), False

    pending = await db.scalar(select(func.count()).select_from(ReserveRequest).where(
        ReserveRequest.bank_code == bank_code, ReserveRequest.status == ReserveRequestStatus.PENDING))
    if pending >= MAX_PENDING:
        raise ApiException(409, "TOO_MANY_PENDING_REQUESTS",
                           f"결정을 기다리는 신청이 이미 {MAX_PENDING}건입니다. 결정되거나 취소한 뒤 다시 신청하세요")
    if kind == ReserveRequestKind.WITHDRAW:
        room = await withdrawable(db, bank_code, await exposure_of(bank_code)) - await _pending_withdraw_total(db, bank_code)
        if amount > room:
            raise _insufficient(room, amount)

    req = ReserveRequest(bank_code=bank_code, request_ref=request_ref, kind=kind, amount=amount, memo=memo,
                         status=ReserveRequestStatus.PENDING)
    try:
        async with db.begin_nested():  # 같은 request_ref가 동시에 들어오면 뒤의 것은 유니크 제약에 걸린다
            db.add(req)
            await db.flush()
    except IntegrityError:
        existing = await _by_ref(db, bank_code, request_ref)
        if existing is None:
            raise
        return _replay(existing, kind, amount, memo), False

    label = "입금" if kind == ReserveRequestKind.DEPOSIT else "인출"
    alerts.notify("reserve_request", f"은행 {bank_code}의 예치금 {label} 신청 {amount:,}원이 승인을 기다립니다",
                  request_id=req.request_id, bank_code=bank_code)
    return req, True


async def get_for_bank(db: AsyncSession, bank_code: str, request_id: int) -> ReserveRequest:
    """내 신청 하나. 다른 은행의 신청이면 없는 것과 같이 404."""
    req = await db.get(ReserveRequest, request_id)
    if req is None or req.bank_code != bank_code:
        raise _not_found(request_id)
    return req


async def list_for_bank(db: AsyncSession, bank_code: str) -> list[ReserveRequest]:
    """최근 30일 신청, 최신순, 최대 100건 (은행 API는 쿼리스트링을 받지 않는다, SPEC 2.2)."""
    stmt = (select(ReserveRequest)
            .where(ReserveRequest.bank_code == bank_code,
                   ReserveRequest.created_at >= _now() - timedelta(days=BANK_LIST_DAYS))
            .order_by(ReserveRequest.request_id.desc()).limit(BANK_LIST_LIMIT))
    return list((await db.scalars(stmt)).all())


async def _lock(db: AsyncSession, request_id: int) -> ReserveRequest:
    stmt = (select(ReserveRequest).where(ReserveRequest.request_id == request_id)
            .with_for_update().execution_options(populate_existing=True))
    req = await db.scalar(stmt)
    if req is None:
        raise _not_found(request_id)
    return req


def _decide(req: ReserveRequest, status: ReserveRequestStatus, by: str) -> None:
    req.status, req.decided_at, req.decided_by = status, _now(), by


async def cancel(db: AsyncSession, bank_code: str, request_id: int) -> ReserveRequest:
    """은행이 결정 전(PENDING)의 내 신청을 취소한다. 이미 결정됐으면 409 RESERVE_REQUEST_NOT_PENDING."""
    req = await _lock(db, request_id)
    if req.bank_code != bank_code:
        raise _not_found(request_id)
    if req.status != ReserveRequestStatus.PENDING:
        raise _not_pending(req)
    _decide(req, ReserveRequestStatus.CANCELLED, BANK)
    await db.flush()
    return req


async def approve(db: AsyncSession, request_id: int, exposure_of: ExposureOf, memo: str = "") -> ReserveRequest:
    """승인: 입금은 DEPOSIT, 인출은 WITHDRAW 분개를 남기고 entry_id를 적는다 (한 트랜잭션).

    인출은 예치금 계좌를 먼저 잠근 뒤 미정산 순채무를 잰다. 이체 접수도 같은 계좌를 잠그므로(service.lock_for_check)
    잰 값과 분개 사이에 새 송금이 끼지 못한다. 인출할 수 있는 금액보다 크면 400 INSUFFICIENT_RESERVE이고
    신청은 PENDING으로 남는다 (관리자가 거절하거나 정산 뒤 다시 승인한다)."""
    req = await _lock(db, request_id)
    if req.status != ReserveRequestStatus.PENDING:
        raise _not_pending(req)
    note = f"신청 #{req.request_id} ({req.request_ref})" + (f" {req.memo}" if req.memo else "") + \
        (f" / {memo}" if memo else "")
    note = note[:200]
    if req.kind == ReserveRequestKind.DEPOSIT:
        entry = await service.deposit(db, req.bank_code, req.amount, note)
    else:
        await service.lock_for_check(db, req.bank_code)
        room = await withdrawable(db, req.bank_code, await exposure_of(req.bank_code))
        if req.amount > room:
            raise _insufficient(room, req.amount)
        entry = await service.withdraw(db, req.bank_code, req.amount, note)
    req.entry_id = entry.entry_id
    _decide(req, ReserveRequestStatus.APPROVED, service.ADMIN)
    await db.flush()
    return req


async def reject(db: AsyncSession, request_id: int, reason: str) -> ReserveRequest:
    req = await _lock(db, request_id)
    if req.status != ReserveRequestStatus.PENDING:
        raise _not_pending(req)
    req.reject_reason = reason
    _decide(req, ReserveRequestStatus.REJECTED, service.ADMIN)
    await db.flush()
    return req


async def list_admin(db: AsyncSession, status: str | None = None, bank_code: str | None = None,
                     limit: int = 100) -> list[ReserveRequest]:
    """관리자 목록. 대기(PENDING)는 오래된 것부터(먼저 처리할 순서), 나머지는 최신순."""
    stmt = select(ReserveRequest)
    if status:
        stmt = stmt.where(ReserveRequest.status == status)
    if bank_code:
        stmt = stmt.where(ReserveRequest.bank_code == bank_code)
    order = ReserveRequest.request_id.asc() if status == ReserveRequestStatus.PENDING else ReserveRequest.request_id.desc()
    return list((await db.scalars(stmt.order_by(order).limit(min(max(limit, 1), 500)))).all())


async def balances_after(db: AsyncSession, reqs: list[ReserveRequest]) -> dict[int, int]:
    """entry_id → 그 분개 뒤 잔액 (응답의 balance_after)."""
    ids = [r.entry_id for r in reqs if r.entry_id is not None]
    if not ids:
        return {}
    rows = await db.execute(select(ReserveLedgerEntry.entry_id, ReserveLedgerEntry.balance_after)
                            .where(ReserveLedgerEntry.entry_id.in_(ids)))
    return dict(rows.all())
