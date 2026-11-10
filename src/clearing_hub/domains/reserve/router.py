from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.database import get_db
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.reserve import requests, service
from clearing_hub.domains.reserve.models import ReserveLedgerEntry, ReserveRequest, ReserveRequestStatus
from clearing_hub.domains.reserve.schemas import (
    LedgerEntryOut, ReserveAdjust, ReserveAdmin, ReserveMovement, ReservePosted, ReserveRequestAdminOut,
    ReserveRequestApprove, ReserveRequestIn, ReserveRequestList, ReserveRequestOut, ReserveRequestReject,
    ReserveSummary,
)
# 미정산 순채무는 transfers 쪽이 계산한다. reserve.service는 transfers를 모르고 값만 받는다 (SETTLEMENT_PLAN 2절 1번).
# 두 도메인을 잇는 것은 이 라우터 층뿐이라, 예치금 원장을 별도 서버로 뗄 때는 여기서 값을 넘기는 방법만 바꾸면 된다
from clearing_hub.domains.transfers.exposure import open_exposure

router = APIRouter(prefix="/v1/reserve", tags=["reserve"])
admin_router = APIRouter(prefix="/admin/reserve", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]
Me = Annotated[Bank, Depends(current_bank)]


def _exposure_of(db: AsyncSession):
    async def of(bank_code: str) -> int:
        return await open_exposure(db, bank_code)
    return of


@router.get("", response_model=ReserveSummary)
async def my_reserve(db: DB, bank: Annotated[Bank, Depends(current_bank)]):
    """내 예치금 (SETTLEMENT_PLAN 6.1). 요청한 은행 자신의 것만 보여 준다."""
    result = await service.summary(db, bank.bank_code, await open_exposure(db, bank.bank_code))
    await db.commit()  # 계좌가 없던 은행이면 방금 만든 계좌를 남긴다 (current_bank가 트랜잭션을 이미 열었다)
    return result


@admin_router.get("", response_model=list[ReserveAdmin])
async def admin_list_reserve(db: DB):
    """은행별 예치금·한도·미정산 순채무·상태(OK / OVERDRAWN / OVERDUE). 모의 예치금이며 실제 돈과는 무관하다 (SETTLEMENT_PLAN 10절).
    순채무는 은행마다 한 번씩 계산한다 (참가 은행이 몇 개뿐이라 N번 쿼리로 충분하다)."""
    banks = await bank_service.list_banks(db, active_only=False)
    return await service.list_admin(db, {b.bank_code: await open_exposure(db, b.bank_code) for b in banks})


@admin_router.get("/ledger", response_model=list[LedgerEntryOut])
async def admin_ledger_all(db: DB, limit: int = 100):
    """전체 은행의 분개, 최신순 (`limit` 기본 100, 최대 500)."""
    return [LedgerEntryOut.model_validate(e, from_attributes=True) for e in await service.ledger_all(db, limit)]


async def _posted(db: AsyncSession, entry: ReserveLedgerEntry) -> ReservePosted:
    return ReservePosted(entry=LedgerEntryOut.model_validate(entry, from_attributes=True),
                         reserve=ReserveSummary(**await service.summary(db, entry.bank_code,
                                                                        await open_exposure(db, entry.bank_code))))


@admin_router.post("/{bank_code}/deposit", response_model=ReservePosted)
async def admin_deposit(bank_code: str, data: ReserveMovement, db: DB):
    """예치 (DEPOSIT 분개)."""
    async with db.begin():
        return await _posted(db, await service.deposit(db, bank_code, data.amount, data.memo))


@admin_router.post("/{bank_code}/withdraw", response_model=ReservePosted)
async def admin_withdraw(bank_code: str, data: ReserveMovement, db: DB):
    """인출 (WITHDRAW 분개). 잔액 − 금액 < −순채무한도면 400 INSUFFICIENT_RESERVE."""
    async with db.begin():
        return await _posted(db, await service.withdraw(db, bank_code, data.amount, data.memo))


@admin_router.post("/{bank_code}/adjust", response_model=ReservePosted)
async def admin_adjust(bank_code: str, data: ReserveAdjust, db: DB):
    """정정 분개 (ADJUST, 양수·음수, 사유 필수). 한도 검사를 하지 않는다."""
    async with db.begin():
        return await _posted(db, await service.adjust(db, bank_code, data.amount, data.memo))


@admin_router.get("/{bank_code}/ledger", response_model=list[LedgerEntryOut])
async def admin_ledger(bank_code: str, db: DB, limit: int = 100):
    """분개 목록, 최신순."""
    return [LedgerEntryOut.model_validate(e, from_attributes=True) for e in await service.ledger(db, bank_code, limit)]


# --- 입금·인출 신청 (SPEC 10.9) ---

def _out(req: ReserveRequest, balances: dict[int, int]) -> dict:
    return {"request_id": req.request_id, "request_ref": req.request_ref, "kind": req.kind, "amount": req.amount,
            "memo": req.memo, "status": req.status, "reject_reason": req.reject_reason, "entry_id": req.entry_id,
            "balance_after": balances.get(req.entry_id) if req.entry_id else None, "created_at": req.created_at,
            "decided_at": req.decided_at}


async def _bank_out(db: AsyncSession, req: ReserveRequest) -> ReserveRequestOut:
    return ReserveRequestOut(**_out(req, await requests.balances_after(db, [req])))


async def _admin_out(db: AsyncSession, req: ReserveRequest) -> ReserveRequestAdminOut:
    return ReserveRequestAdminOut(**_out(req, await requests.balances_after(db, [req])), bank_code=req.bank_code,
                                  decided_by=req.decided_by)


@router.post("/requests", response_model=ReserveRequestOut, status_code=201)
async def create_request(data: ReserveRequestIn, db: DB, bank: Me, response: Response):
    """입금·인출 신청 (SPEC 10.9). 새 신청은 201, 같은 request_ref·같은 내용을 다시 보내면 200과 기존 신청.
    잔액은 바뀌지 않는다. 허브 관리자가 승인하면 그때 예치금이 바뀐다."""
    # current_bank가 이미 세션을 썼으므로(autobegin) begin() 대신 commit한다. 예외면 커밋하지 않고 롤백된다
    req, created = await requests.create(db, bank.bank_code, data.request_ref, data.kind, data.amount, data.memo,
                                         _exposure_of(db))
    await db.commit()
    if not created:
        response.status_code = 200
    return await _bank_out(db, req)


@router.get("/requests", response_model=ReserveRequestList)
async def my_requests(db: DB, bank: Me):
    """내 신청 목록: 최근 30일, 최신순, 최대 100건."""
    reqs = await requests.list_for_bank(db, bank.bank_code)
    balances = await requests.balances_after(db, reqs)
    return ReserveRequestList(requests=[ReserveRequestOut(**_out(r, balances)) for r in reqs])


@router.get("/requests/{request_id}", response_model=ReserveRequestOut)
async def my_request(request_id: int, db: DB, bank: Me):
    """내 신청 하나. 은행은 이것을 주기적으로 읽어 결정(APPROVED/REJECTED)을 확인한다."""
    return await _bank_out(db, await requests.get_for_bank(db, bank.bank_code, request_id))


@router.post("/requests/{request_id}/cancel", response_model=ReserveRequestOut)
async def cancel_request(request_id: int, db: DB, bank: Me):
    """결정 전(PENDING) 신청 취소. 이미 결정됐으면 409 RESERVE_REQUEST_NOT_PENDING."""
    req = await requests.cancel(db, bank.bank_code, request_id)
    await db.commit()
    return await _bank_out(db, req)


@admin_router.get("/requests", response_model=list[ReserveRequestAdminOut])
async def admin_list_requests(db: DB, status: Annotated[ReserveRequestStatus | None, Query()] = None,
                              bank_code: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    """신청 목록. `status=PENDING`이면 오래된 것부터, 그 밖에는 최신순."""
    reqs = await requests.list_admin(db, status, bank_code, limit)
    balances = await requests.balances_after(db, reqs)
    return [ReserveRequestAdminOut(**_out(r, balances), bank_code=r.bank_code, decided_by=r.decided_by) for r in reqs]


@admin_router.post("/requests/{request_id}/approve", response_model=ReserveRequestAdminOut)
async def admin_approve_request(request_id: int, db: DB, data: ReserveRequestApprove | None = None):
    """승인: 입금은 DEPOSIT, 인출은 WITHDRAW 분개를 남긴다. 인출할 수 있는 금액(잔액 − 미정산 순채무)보다 큰
    인출이면 400 INSUFFICIENT_RESERVE이고 신청은 PENDING으로 남는다. 결정된 신청이면 409 RESERVE_REQUEST_NOT_PENDING."""
    async with db.begin():
        req = await requests.approve(db, request_id, _exposure_of(db), data.memo if data else "")
        return await _admin_out(db, req)


@admin_router.post("/requests/{request_id}/reject", response_model=ReserveRequestAdminOut)
async def admin_reject_request(request_id: int, data: ReserveRequestReject, db: DB):
    """거절 (사유 필수, 은행에 그대로 보인다). 잔액은 바뀌지 않는다."""
    async with db.begin():
        return await _admin_out(db, await requests.reject(db, request_id, data.reason))
