"""예치금 원장 (SETTLEMENT_PLAN 2절 5·7번, 5.1의 12단계).

커밋은 하지 않는다. 라우터(`async with db.begin()`)나 정산 서비스가 트랜잭션을 정한다.
잔액이 바뀌는 함수는 모두 `_post`를 거치고, `_post`는 계좌 행을 `SELECT ... FOR UPDATE`로 잠근 뒤
잔액과 분개를 함께 바꾼다 (SQLite는 FOR UPDATE를 무시한다. 테스트는 연결 하나라 상관없다).
"""

import logging
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.exceptions import ApiException, BadRequest, NotFound
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.reserve.models import PositionStatus, ReserveAccount, ReserveLedgerEntry, ReserveLedgerKind

logger = logging.getLogger(__name__)

ADMIN = "admin"
SETTLEMENT = "settlement"


@dataclass(frozen=True)
class PositionResult:
    """정산 지시서 한 줄을 반영한 결과. 4단계에서 settlement_positions.result·balance_after에 옮겨 적는다."""

    bank_code: str
    net_amount: int
    status: PositionStatus
    balance_after: int


def _invalid(message: str) -> ApiException:
    return ApiException(422, "VALIDATION_ERROR", message)


async def _require_bank(db: AsyncSession, bank_code: str) -> Bank:
    bank = await db.get(Bank, bank_code)
    if bank is None:
        raise NotFound("BANK_NOT_FOUND", f"은행 코드 {bank_code}를 찾을 수 없습니다")
    return bank


async def _ensure_account(db: AsyncSession, bank_code: str) -> Bank:
    """은행을 확인하고, 예치금 계좌가 없으면 잔액 0으로 만든다 (이 기능 전에 등록된 은행용).
    두 요청이 동시에 만들어도 충돌하지 않게 INSERT ... ON CONFLICT DO NOTHING을 쓴다."""
    bank = await _require_bank(db, bank_code)
    if await db.get(ReserveAccount, bank_code) is None:
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        await db.execute(insert(ReserveAccount).values(bank_code=bank_code, balance=0).on_conflict_do_nothing())
    return bank


async def get_or_create(db: AsyncSession, bank_code: str) -> ReserveAccount:
    """예치금 계좌. 없으면 만든다. 은행이 없으면 404 BANK_NOT_FOUND."""
    await _ensure_account(db, bank_code)
    return await db.get(ReserveAccount, bank_code)


async def _lock(db: AsyncSession, bank_code: str) -> ReserveAccount:
    """계좌 행 잠금 (없으면 만든 뒤). 이미 읽어 둔 객체도 잠근 시점의 잔액으로 다시 채운다 (populate_existing)."""
    await _ensure_account(db, bank_code)
    stmt = (select(ReserveAccount).where(ReserveAccount.bank_code == bank_code)
            .with_for_update().execution_options(populate_existing=True))
    return await db.scalar(stmt)


async def _post(db: AsyncSession, account: ReserveAccount, kind: ReserveLedgerKind, amount: int, *,
                run_id: int | None = None, memo: str = "", created_by: str) -> ReserveLedgerEntry:
    """잔액 갱신 + 분개 + balance_after (모든 변동의 공통 경로). 행을 잠근 뒤 바꾼다.
    호출한 쪽이 한도 검사를 위해 먼저 잠갔어도, 같은 트랜잭션에서 다시 잠그는 것은 대기 없이 지나간다."""
    account = await _lock(db, account.bank_code)
    account.balance += amount
    entry = ReserveLedgerEntry(bank_code=account.bank_code, kind=kind, amount=amount, run_id=run_id,
                               balance_after=account.balance, memo=memo, created_by=created_by)
    db.add(entry)
    await db.flush()
    return entry


async def deposit(db: AsyncSession, bank_code: str, amount: int, memo: str = "") -> ReserveLedgerEntry:
    """예치 (DEPOSIT). 모의 예치금이라 실제 돈은 움직이지 않는다."""
    if amount < 1:
        raise _invalid("amount는 1 이상이어야 합니다")
    account = await get_or_create(db, bank_code)
    return await _post(db, account, ReserveLedgerKind.DEPOSIT, amount, memo=memo, created_by=ADMIN)


async def withdraw(db: AsyncSession, bank_code: str, amount: int, memo: str = "") -> ReserveLedgerEntry:
    """인출 (WITHDRAW). 잔액 − 금액 < −순채무한도면 400 INSUFFICIENT_RESERVE (SETTLEMENT_PLAN 6.2)."""
    if amount < 1:
        raise _invalid("amount는 1 이상이어야 합니다")
    bank = await _require_bank(db, bank_code)
    account = await _lock(db, bank_code)  # 검사와 반영 사이에 잔액이 바뀌지 않게 먼저 잠근다
    if account.balance - amount < -bank.net_debit_cap:
        raise BadRequest("INSUFFICIENT_RESERVE",
                         f"예치금이 부족합니다 (잔액 {account.balance}원, 한도 {bank.net_debit_cap}원)")
    return await _post(db, account, ReserveLedgerKind.WITHDRAW, -amount, memo=memo, created_by=ADMIN)


async def adjust(db: AsyncSession, bank_code: str, amount: int, memo: str) -> ReserveLedgerEntry:
    """정정 (ADJUST). 양수·음수 모두, 한도 검사 없음 (관리자 정정). 사유 필수 (SETTLEMENT_PLAN 2절 8번)."""
    if amount == 0:
        raise _invalid("amount는 0일 수 없습니다")
    if not memo.strip():
        raise _invalid("정정 사유(memo)를 넣어야 합니다")
    account = await get_or_create(db, bank_code)
    return await _post(db, account, ReserveLedgerKind.ADJUST, amount, memo=memo, created_by=ADMIN)


def position_status(balance: int, cap: int) -> PositionStatus:
    """OK(잔액 ≥ 0) / OVERDRAWN(−한도 ≤ 잔액 < 0) / OVERDUE(잔액 < −한도) (SETTLEMENT_PLAN 2절 7번)."""
    if balance >= 0:
        return PositionStatus.OK
    return PositionStatus.OVERDRAWN if balance >= -cap else PositionStatus.OVERDUE


async def apply_instruction(db: AsyncSession, run_id: int, instruction: list[tuple[str, int]],
                            created_by: str = SETTLEMENT) -> list[PositionResult]:
    """정산 지시서 반영 (SETTLEMENT_PLAN 5.1의 12단계: 중앙은행 원장 반영).

    **경계**: 입력은 `(run_id, [(bank_code, net_amount)])`뿐이다. 이 함수는 `transfers`·`settlement_*` 테이블을
    전혀 모르고 읽지도 않는다. 정산 도메인이 포지션에서 지시서를 만들어 넘긴다. 이 함수를 HTTP 호출로 바꾸면
    예치금 원장을 별도 서버(중앙은행 역할)로 뗄 수 있다 (SETTLEMENT_PLAN 2절 1번).

    은행마다 balance += net_amount. 양수는 SETTLE_CREDIT, 음수는 SETTLE_DEBIT, 0은 분개 없이 결과만 OK.
    잔액이 −순채무한도보다 작아져도 반영하고 결과를 OVERDUE로 둔다 (2절 7번). 결과는 은행 코드순.

    같은 run_id는 한 번만 반영된다. 이미 분개가 있으면, 또는 동시에 반영돼 유니크 제약(uq_reserve_ledger_run)에
    걸리면 409 RESERVE_ALREADY_APPLIED. 오류가 나면 호출한 쪽이 트랜잭션을 롤백해야 잔액 변경도 함께 취소된다.
    지시서 자체가 틀리면(같은 은행 두 번, 합이 0이 아님) 정산 계산 버그이므로 ValueError.
    """
    codes = [code for code, _ in instruction]
    if len(set(codes)) != len(codes):
        raise ValueError(f"정산 지시서에 같은 은행이 두 번 있습니다 (run_id={run_id}): {codes}")
    if sum(amount for _, amount in instruction) != 0:  # 다자간 차액의 합은 항상 0 (2절 2번)
        raise ValueError(f"정산 지시서의 합이 0이 아닙니다 (run_id={run_id})")
    if await _already_applied(db, run_id):
        raise _already_applied_error(run_id)
    banks = {code: await _require_bank(db, code) for code in sorted(codes)}  # 하나라도 없으면 아무것도 바꾸기 전에 404

    results: list[PositionResult] = []
    try:
        for bank_code, net_amount in sorted(instruction):  # 잠그는 순서를 은행 코드로 고정한다 (교착 방지)
            bank = banks[bank_code]
            account = await _lock(db, bank_code)
            if net_amount != 0:
                kind = ReserveLedgerKind.SETTLE_CREDIT if net_amount > 0 else ReserveLedgerKind.SETTLE_DEBIT
                await _post(db, account, kind, net_amount, run_id=run_id, memo=f"정산 run {run_id}",
                            created_by=created_by)
            results.append(PositionResult(bank_code, net_amount, position_status(account.balance, bank.net_debit_cap),
                                          account.balance))
    except IntegrityError as exc:
        raise _already_applied_error(run_id) from exc

    for r in results:
        if r.status == PositionStatus.OVERDUE:
            logger.error("정산 run %s: 은행 %s 예치금 부족 (잔액 %s원, 순채무한도 초과)", run_id, r.bank_code,
                         r.balance_after)
    return results


async def _already_applied(db: AsyncSession, run_id: int) -> bool:
    return bool(await db.scalar(select(exists().where(ReserveLedgerEntry.run_id == run_id))))


def _already_applied_error(run_id: int) -> ApiException:
    return ApiException(409, "RESERVE_ALREADY_APPLIED", f"정산 run {run_id}은(는) 이미 예치금에 반영됐습니다")


async def ledger(db: AsyncSession, bank_code: str, limit: int = 100) -> list[ReserveLedgerEntry]:
    """분개 목록, 최신순 (최대 500건)."""
    await _require_bank(db, bank_code)
    stmt = (select(ReserveLedgerEntry).where(ReserveLedgerEntry.bank_code == bank_code)
            .order_by(ReserveLedgerEntry.entry_id.desc()).limit(min(max(limit, 1), 500)))
    return list((await db.scalars(stmt)).all())


def _summary(bank: Bank, balance: int, exposure: int) -> dict:
    return {"bank_code": bank.bank_code, "balance": balance, "net_debit_cap": bank.net_debit_cap, "exposure": exposure,
            "available": balance + bank.net_debit_cap - exposure}


async def summary(db: AsyncSession, bank_code: str) -> dict:
    """balance, net_debit_cap, exposure, available = balance + net_debit_cap − exposure (SETTLEMENT_PLAN 6.1)."""
    bank = await _ensure_account(db, bank_code)
    account = await db.get(ReserveAccount, bank_code)
    # TODO 2단계: 미정산 순채무(SETTLEMENT_PLAN 4.4). 이 도메인은 transfers를 읽지 않으므로(2절 1번)
    #   transfers 쪽 함수가 계산한 값을 인자로 받는 방식으로 넣는다. 지금은 0
    exposure = 0
    return _summary(bank, account.balance, exposure)


async def list_admin(db: AsyncSession) -> list[dict]:
    """등록된 은행 전부의 예치금. 계좌가 아직 없는 은행은 잔액 0으로 보여 주고 만들지는 않는다."""
    stmt = (select(Bank, ReserveAccount).outerjoin(ReserveAccount, ReserveAccount.bank_code == Bank.bank_code)
            .order_by(Bank.bank_code))
    rows = (await db.execute(stmt)).all()
    # TODO 2단계: exposure (summary와 같은 방식)
    return [{**_summary(bank, account.balance if account else 0, 0), "updated_at": account.updated_at if account else None}
            for bank, account in rows]
