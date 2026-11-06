from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.database import get_db
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.reserve import service
from clearing_hub.domains.reserve.models import ReserveLedgerEntry
from clearing_hub.domains.reserve.schemas import (
    LedgerEntryOut, ReserveAdjust, ReserveAdmin, ReserveMovement, ReservePosted, ReserveSummary,
)

router = APIRouter(prefix="/v1/reserve", tags=["reserve"])
admin_router = APIRouter(prefix="/admin/reserve", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=ReserveSummary)
async def my_reserve(db: DB, bank: Annotated[Bank, Depends(current_bank)]):
    """내 예치금 (SETTLEMENT_PLAN 6.1). 요청한 은행 자신의 것만 보여 준다."""
    result = await service.summary(db, bank.bank_code)
    await db.commit()  # 계좌가 없던 은행이면 방금 만든 계좌를 남긴다 (current_bank가 트랜잭션을 이미 열었다)
    return result


@admin_router.get("", response_model=list[ReserveAdmin])
async def admin_list_reserve(db: DB):
    """은행별 예치금·한도. 모의 예치금이며 실제 돈과는 무관하다 (SETTLEMENT_PLAN 10절)."""
    return await service.list_admin(db)


async def _posted(db: AsyncSession, entry: ReserveLedgerEntry) -> ReservePosted:
    return ReservePosted(entry=LedgerEntryOut.model_validate(entry, from_attributes=True),
                         reserve=ReserveSummary(**await service.summary(db, entry.bank_code)))


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
