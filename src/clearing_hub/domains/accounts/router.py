import json
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.database import get_db
from clearing_hub.domains.accounts import service
from clearing_hub.domains.accounts.models import AccountLookup
from clearing_hub.domains.accounts.schemas import LookupRequest, LookupResponse
from clearing_hub.domains.banks.models import Bank

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])
admin_router = APIRouter(prefix="/admin/account-lookups", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.post("/lookup", response_model=LookupResponse)
async def lookup(data: LookupRequest, db: DB, requester: Annotated[Bank, Depends(current_bank)]):
    """본인 명의 계좌 조회 (SPEC 9.2). 고객 동의를 받은 뒤 부른다. 일부 은행이 실패해도 200."""
    return await service.lookup(db, requester, data)


@admin_router.get("")
async def admin_list_lookups(db: DB, from_bank: str | None = None, limit: int = 100):
    """조회 기록 (SPEC 9.5). 은행에 동의 기록을 요구할 때 consent_id로 맞춰 본다."""
    stmt = select(AccountLookup).order_by(AccountLookup.created_at.desc()).limit(min(limit, 500))
    if from_bank:
        stmt = stmt.where(AccountLookup.from_bank == from_bank)
    return [{"lookup_id": r.lookup_id, "from_bank": r.from_bank, "consent_id": r.consent_id, "account_count": r.account_count,
             "banks": json.loads(r.results), "created_at": r.created_at} for r in (await db.scalars(stmt)).all()]
