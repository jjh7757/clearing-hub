"""연동 진단 API (SPEC 3.8, SETTLEMENT_PLAN 14절). 모두 읽기 전용이고 규칙 진단만 한다.

은행용 API는 **서명 없이** `X-Bank-Code` + `X-Api-Key`만으로 식별한다 (`identified_bank`). 서명이 안 돼서 막힌 은행이
원인을 보려고 부르는 API라서다. 보여 주는 것은 그 은행으로 식별된 자기 실패뿐이고, 아무것도 바꾸지 않는다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import identified_bank, require_admin
from clearing_hub.core.database import get_db
from clearing_hub.core.exceptions import NotFound
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.support import service
from clearing_hub.domains.support.schemas import FailureList

router = APIRouter(prefix="/v1/diagnose", tags=["support"])
admin_router = APIRouter(prefix="/admin/banks", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]
Identified = Annotated[Bank, Depends(identified_bank)]


@router.get("/failures", response_model=FailureList)
async def my_failures(db: DB, bank: Identified):
    """내 최근 인증 실패 20건과 규칙 진단 (횟수 제한 없음). 401 본문의 hint로 모르겠으면 이것을 본다 (SPEC 7절 8)."""
    return FailureList(bank_code=bank.bank_code, findings=await service.failure_findings(db, bank, service.LIST_FAILURES))


@admin_router.get("/{bank_code}/auth-failures", response_model=FailureList)
async def admin_auth_failures(bank_code: str, db: DB, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """이 은행의 최근 인증 실패와 규칙 진단. API 키가 틀려 식별되지 않았지만 X-Bank-Code가 이 은행이었던 실패도 넣는다.
    팀이 "401이 나요"라고 하면 이 결과를 그대로 보내 준다 (docs/ONBOARDING.md 7)."""
    bank = await bank_service.get_bank(db, bank_code)
    if bank is None:
        raise NotFound("BANK_NOT_FOUND", "없는 은행 코드입니다")
    return FailureList(bank_code=bank_code, findings=await service.failure_findings(db, bank, limit, include_claimed=True))
