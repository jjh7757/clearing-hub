from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.bank_client import bank_client
from clearing_hub.core.database import get_db
from clearing_hub.domains.banks import service
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.banks.schemas import BankAdmin, BankCreate, BankCredentials, BankList, BankPublic, BankUpdate

router = APIRouter(prefix="/v1/banks", tags=["banks"])
admin_router = APIRouter(prefix="/admin/banks", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=BankList)
async def list_banks(db: DB, _: Annotated[Bank, Depends(current_bank)]):
    """참가 은행 목록 (SPEC 3.1). 화면의 '받는 은행' 선택용."""
    return BankList(banks=[BankPublic.model_validate(b, from_attributes=True) for b in await service.list_banks(db)])


@admin_router.get("", response_model=list[BankAdmin])
async def admin_list_banks(db: DB):
    return [BankAdmin.model_validate(b, from_attributes=True) for b in await service.list_banks(db, active_only=False)]


@admin_router.post("", response_model=BankCredentials, status_code=201)
async def admin_register_bank(data: BankCreate, db: DB):
    """은행 등록 + API 키·secret 발급 (한 번만 보여 준다)."""
    async with db.begin():
        bank, api_key, secret = await service.register_bank(db, data)
    return BankCredentials(**BankAdmin.model_validate(bank, from_attributes=True).model_dump(), api_key=api_key, secret=secret)


@admin_router.patch("/{bank_code}", response_model=BankAdmin)
async def admin_update_bank(bank_code: str, data: BankUpdate, db: DB):
    """이름·base_url 변경 (은행 주소가 바뀌었을 때). 바꾼 뒤 /ping으로 확인한다."""
    async with db.begin():
        bank = await service.update_bank(db, bank_code, data)
    return BankAdmin.model_validate(bank, from_attributes=True)


@admin_router.post("/{bank_code}/keys", response_model=BankCredentials)
async def admin_rotate_keys(bank_code: str, db: DB):
    async with db.begin():
        bank, api_key, secret = await service.rotate_keys(db, bank_code)
    return BankCredentials(**BankAdmin.model_validate(bank, from_attributes=True).model_dump(), api_key=api_key, secret=secret)


@admin_router.patch("/{bank_code}/active", response_model=BankAdmin)
async def admin_set_active(bank_code: str, active: bool, db: DB):
    async with db.begin():
        bank = await service.set_active(db, bank_code, active)
    return BankAdmin.model_validate(bank, from_attributes=True)


@admin_router.post("/{bank_code}/ping")
async def admin_ping(bank_code: str, db: DB):
    """연결 상태 확인: 은행의 /inquiry를 불러 본다 (SPEC 7-4). 계좌가 없어도 404면 연결·서명은 된 것."""
    bank = await service.require_active_bank(db, bank_code)
    reply = await bank_client.inquiry(bank, "000-000-000000")
    return {"bank_code": bank_code, "reachable": reply.status is not None, "status": reply.status, "body": reply.body}
