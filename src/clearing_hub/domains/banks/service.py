from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.exceptions import ApiException, BadRequest, NotFound
from clearing_hub.core.signing import hash_api_key, new_api_key, new_secret
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.banks.schemas import BankCreate, BankUpdate


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
    await db.flush()
    return bank, api_key, secret


async def _require_bank(db: AsyncSession, bank_code: str) -> Bank:
    bank = await get_bank(db, bank_code)
    if bank is None:
        raise NotFound("BANK_NOT_FOUND", f"은행 코드 {bank_code}를 찾을 수 없습니다")
    return bank


async def update_bank(db: AsyncSession, bank_code: str, data: BankUpdate) -> Bank:
    """이름·base_url 변경. 진행 중인 재시도도 다음 호출부터 새 주소로 간다 (거래에는 주소를 저장하지 않는다)."""
    bank = await _require_bank(db, bank_code)
    if data.name is not None:
        bank.name = data.name
    if data.base_url is not None:
        bank.base_url = str(data.base_url).rstrip("/")
    await db.flush()
    return bank


async def rotate_keys(db: AsyncSession, bank_code: str) -> tuple[Bank, str, str]:
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
