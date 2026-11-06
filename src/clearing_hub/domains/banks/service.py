from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.config import get_settings
from clearing_hub.core.exceptions import ApiException, BadRequest, NotFound
from clearing_hub.core.signing import hash_api_key, new_api_key, new_secret
from clearing_hub.domains.banks.models import Bank, SignatureMode
from clearing_hub.domains.banks.schemas import BankCreate, BankUpdate
from clearing_hub.domains.reserve.models import ReserveAccount


async def get_bank(db: AsyncSession, bank_code: str) -> Bank | None:
    return await db.get(Bank, bank_code)


async def require_active_bank(db: AsyncSession, bank_code: str, transfer_id: str | None = None) -> Bank:
    """받는 은행 확인 (BANK_NOT_FOUND / BANK_INACTIVE)."""
    bank = await get_bank(db, bank_code)
    if bank is None:
        raise NotFound("BANK_NOT_FOUND", f"은행 코드 {bank_code}를 찾을 수 없습니다", transfer_id)
    if not bank.active:
        raise BadRequest("BANK_INACTIVE", f"{bank.name}은(는) 비활성 상태입니다", transfer_id)
    return bank


async def list_banks(db: AsyncSession, active_only: bool = True) -> list[Bank]:
    stmt = select(Bank).order_by(Bank.bank_code)
    if active_only:
        stmt = stmt.where(Bank.active.is_(True))
    return list((await db.scalars(stmt)).all())


async def register_bank(db: AsyncSession, data: BankCreate) -> tuple[Bank, str, str]:
    """은행 등록. (bank, api_key, secret)을 돌려준다. 키는 이때만 볼 수 있다."""
    if await get_bank(db, data.bank_code):
        raise ApiException(409, "BANK_EXISTS", f"은행 코드 {data.bank_code}는 이미 있습니다")
    api_key, secret = new_api_key(), new_secret()
    bank = Bank(
        bank_code=data.bank_code, name=data.name, base_url=str(data.base_url).rstrip("/"),
        api_key_hash=hash_api_key(api_key), hmac_secret=secret,
    )
    db.add(bank)
    await db.flush()  # reserve_accounts가 banks를 참조하므로 은행을 먼저 넣는다
    db.add(ReserveAccount(bank_code=bank.bank_code, balance=0))  # 예치금 계좌 (잔액 0, SETTLEMENT_PLAN 2절 5번)
    await db.flush()
    return bank, api_key, secret


async def _require_bank(db: AsyncSession, bank_code: str) -> Bank:
    bank = await get_bank(db, bank_code)
    if bank is None:
        raise NotFound("BANK_NOT_FOUND", f"은행 코드 {bank_code}를 찾을 수 없습니다")
    return bank


async def update_bank(db: AsyncSession, bank_code: str, data: BankUpdate) -> Bank:
    """이름·base_url·순채무한도 변경. 진행 중인 재시도도 다음 호출부터 새 주소로 간다 (거래에는 주소를 저장하지 않는다)."""
    bank = await _require_bank(db, bank_code)
    if data.name is not None:
        bank.name = data.name
    if data.base_url is not None:
        bank.base_url = str(data.base_url).rstrip("/")
    if data.net_debit_cap is not None:
        bank.net_debit_cap = data.net_debit_cap
    await db.flush()
    return bank


async def rotate_keys(db: AsyncSession, bank_code: str) -> tuple[Bank, str, str]:
    """API 키와 HMAC secret만 새로 만든다. Ed25519 공개키·signature_mode는 그대로다 (공개키는 은행이 PUT /v1/keys로 바꾼다)."""
    bank = await _require_bank(db, bank_code)
    api_key, secret = new_api_key(), new_secret()
    bank.api_key_hash, bank.hmac_secret = hash_api_key(api_key), secret
    await db.flush()
    return bank, api_key, secret


async def set_active(db: AsyncSession, bank_code: str, active: bool) -> Bank:
    bank = await _require_bank(db, bank_code)
    bank.active = active
    await db.flush()
    return bank


# ---- 공개키·서명 모드 (SETTLEMENT_PLAN 12.2 5·7·8번, SPEC 2.6) ----

async def register_public_key(db: AsyncSession, bank: Bank, public_key: str) -> Bank:
    """공개키 등록·교체. 이미 다른 키가 있으면 그 키를 previous_public_key에 PREVIOUS_KEY_TTL_SECONDS(기본 24시간) 동안
    둔다 (진행 중 재시도가 깨지지 않게). 같은 키를 다시 넣으면 바꾸지 않는다. signature_mode는 바꾸지 않는다 (관리자 몫)."""
    if bank.public_key == public_key:
        return bank
    now = datetime.now(UTC)
    if bank.public_key:
        bank.previous_public_key = bank.public_key
        bank.previous_key_expires_at = now + timedelta(seconds=get_settings().previous_key_ttl_seconds)
    bank.public_key, bank.public_key_registered_at = public_key, now
    await db.flush()
    return bank


async def admin_register_public_key(db: AsyncSession, bank_code: str, public_key: str) -> Bank:
    return await register_public_key(db, await _require_bank(db, bank_code), public_key)


async def set_signature_mode(db: AsyncSession, bank_code: str, mode: SignatureMode) -> Bank:
    """hmac / both / ed25519 전환. ed25519는 은행 공개키와 허브 키(HUB_SIGNING_KEY)가 있어야 한다:
    허브가 그 은행에 Ed25519로 서명하고, 은행의 응답 서명을 공개키로 검증하기 때문이다.
    내리는 것(ed25519 → both·hmac)은 언제든 된다 (개인키 유출 때 바로 막기)."""
    bank = await _require_bank(db, bank_code)
    if mode == SignatureMode.ED25519:
        if not bank.public_key:
            raise BadRequest("PUBLIC_KEY_MISSING", "공개키가 등록되지 않은 은행은 ed25519로 바꿀 수 없습니다")
        if get_settings().hub_signing_key is None:
            raise BadRequest("HUB_KEY_MISSING", "허브 서명 키(HUB_SIGNING_KEY)가 없어 ed25519로 바꿀 수 없습니다")
    bank.signature_mode = mode
    await db.flush()
    return bank
