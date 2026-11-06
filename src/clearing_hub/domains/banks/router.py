from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core.bank_client import bank_client
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import get_db
from clearing_hub.core.exceptions import NotFound
from clearing_hub.core.signing import fingerprint, public_key_of
from clearing_hub.domains.banks import service
from clearing_hub.domains.banks.models import Bank, SignatureMode
from clearing_hub.domains.banks.schemas import (
    BankAdmin, BankCreate, BankCredentials, BankList, BankPublic, BankUpdate, HubKey, KeyStatus, PublicKeyIn, SignatureModeIn,
)

router = APIRouter(prefix="/v1/banks", tags=["banks"])
keys_router = APIRouter(prefix="/v1", tags=["keys"])  # 부인방지 키 (SETTLEMENT_PLAN 12.5, SPEC 2.6)
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
    """이름·base_url·순채무한도 변경. 주소를 바꿨으면 /ping으로 확인한다."""
    async with db.begin():
        bank = await service.update_bank(db, bank_code, data)
    return BankAdmin.model_validate(bank, from_attributes=True)


@admin_router.post("/{bank_code}/keys", response_model=BankCredentials)
async def admin_rotate_keys(bank_code: str, db: DB):
    """API 키·HMAC secret 재발급 (SPEC 2절). Ed25519 공개키와 signature_mode는 바꾸지 않는다.
    공개키는 은행이 PUT /v1/keys로(또는 관리자가 /public-key로) 따로 바꾼다."""
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
    # ed25519 모드에서 응답 서명이 틀리면 BankReply는 "응답 없음"이 되므로 실제로 받은 상태를 따로 보여 준다 (SPEC 7절 4)
    status = reply.exchange.response_status if reply.exchange else reply.status
    return {"bank_code": bank_code, "reachable": status is not None, "status": status, "body": reply.body,
            "signature_mode": bank.signature_mode, "response_signature_valid": reply.signature_valid}


@admin_router.post("/{bank_code}/public-key", response_model=BankAdmin)
async def admin_register_public_key(bank_code: str, data: PublicKeyIn, db: DB):
    """은행이 채팅 등으로 준 공개키를 관리자가 대신 등록한다. 이전 키는 24시간 받는다. 모드는 바꾸지 않는다."""
    async with db.begin():
        bank = await service.admin_register_public_key(db, bank_code, data.public_key)
    return BankAdmin.model_validate(bank, from_attributes=True)


@admin_router.patch("/{bank_code}/signature-mode", response_model=BankAdmin)
async def admin_set_signature_mode(bank_code: str, data: SignatureModeIn, db: DB):
    """hmac → both → ed25519 전환 (SPEC 2.6). ed25519는 은행 공개키·허브 키가 있어야 한다 (없으면 400).
    은행이 받는 쪽(허브 서명 Ed25519 검증, 자기 응답 서명)을 준비했다고 알려 온 뒤에 올린다."""
    async with db.begin():
        bank = await service.set_signature_mode(db, bank_code, SignatureMode(data.signature_mode))
    return BankAdmin.model_validate(bank, from_attributes=True)


def _hub_public_key() -> str | None:
    key = get_settings().hub_signing_key
    return public_key_of(key.get_secret_value()) if key is not None else None


def _key_status(bank: Bank) -> KeyStatus:
    hub_public = _hub_public_key()
    return KeyStatus(
        bank_code=bank.bank_code, fingerprint=bank.public_key_fingerprint, signature_mode=bank.signature_mode,
        registered_at=bank.public_key_registered_at,
        previous_fingerprint=fingerprint(bank.previous_public_key) if bank.previous_public_key else None,
        previous_key_expires_at=bank.previous_key_expires_at,
        hub_fingerprint=fingerprint(hub_public) if hub_public else None,
    )


@keys_router.get("/hub-key", response_model=HubKey)
async def hub_key():
    """허브 Ed25519 공개키 (인증 불필요). 처음 받을 때 허브 관리자가 따로 알려 준 지문과 맞춰 본다 (SPEC 2.6)."""
    public_key = _hub_public_key()
    if public_key is None:
        raise NotFound("HUB_KEY_MISSING", "허브 서명 키가 아직 설정되지 않았습니다")
    return HubKey(public_key=public_key, fingerprint=fingerprint(public_key))


@keys_router.put("/keys", response_model=KeyStatus)
async def put_my_key(data: PublicKeyIn, db: DB, bank: Annotated[Bank, Depends(current_bank)]):
    """내 Ed25519 공개키 등록·교체 (SPEC 2.6). 지금 인증(HMAC, 또는 현재 Ed25519 키)으로 서명한 요청이라 자기 것만 바꾼다.
    이전 키는 24시간 더 받는다. signature_mode는 바뀌지 않는다 (전환은 허브 관리자에게 요청)."""
    # current_bank가 이미 세션을 썼으므로(autobegin) begin() 대신 commit한다
    bank = await service.register_public_key(db, bank, data.public_key)
    await db.commit()
    return _key_status(bank)


@keys_router.get("/keys", response_model=KeyStatus)
async def get_my_key(bank: Annotated[Bank, Depends(current_bank)]):
    """내 키 등록 상태: 지문, signature_mode, 이전 키 만료 시각, 허브 공개키 지문."""
    return _key_status(bank)
