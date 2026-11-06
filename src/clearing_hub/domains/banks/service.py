from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.config import get_settings
from clearing_hub.core.exceptions import ApiException, BadRequest, NotFound
from clearing_hub.core.signing import hash_api_key, new_api_key, new_invite_code
from clearing_hub.domains.banks.models import Bank, Invite
from clearing_hub.domains.banks.schemas import BankCreate, BankRegister, BankUpdate
from clearing_hub.domains.reserve.models import ReserveAccount


async def get_bank(db: AsyncSession, bank_code: str) -> Bank | None:
    return await db.get(Bank, bank_code)


async def require_active_bank(db: AsyncSession, bank_code: str, transfer_id: str | None = None) -> Bank:
    """받는 은행 확인 (BANK_NOT_FOUND / BANK_INACTIVE / PUBLIC_KEY_MISSING).

    공개키가 없는 은행(8c 전에 등록돼 아직 키를 올리지 않은 은행)은 부르지 않는다. 허브가 그 은행의 응답 서명을 검증할 수
    없어서, 입금이 됐어도 "응답 없음"이 되어 PENDING → UNKNOWN으로 쌓이기 때문이다 (SPEC 2.5). 접수 전 거절이라 돈은 안 움직인다."""
    bank = await get_bank(db, bank_code)
    if bank is None:
        raise NotFound("BANK_NOT_FOUND", f"은행 코드 {bank_code}를 찾을 수 없습니다", transfer_id)
    if not bank.active:
        raise BadRequest("BANK_INACTIVE", f"{bank.name}은(는) 비활성 상태입니다", transfer_id)
    if not bank.public_key:
        raise BadRequest("PUBLIC_KEY_MISSING", f"{bank.name}의 공개키가 허브에 등록되지 않아 지금은 보낼 수 없습니다", transfer_id)
    return bank


async def list_banks(db: AsyncSession, active_only: bool = True) -> list[Bank]:
    stmt = select(Bank).order_by(Bank.bank_code)
    if active_only:
        stmt = stmt.where(Bank.active.is_(True))
    return list((await db.scalars(stmt)).all())


def _normalize_url(url) -> str:
    return str(url).rstrip("/")


def normalized_name(name: str) -> str:
    """이름 비교 키: 소문자, 공백 제거. "Avengers Bank"와 "avengersbank"는 같은 이름으로 본다 (SPEC 3.5)."""
    return "".join(name.split()).lower()


async def _require_unique_name(db: AsyncSession, name: str, exclude_code: str | None = None) -> None:
    """같은 이름(공백·대소문자 무시)의 은행이 있으면 409 BANK_NAME_TAKEN. 은행 이름은 다른 은행 화면과 거래내역에
    그대로 나오므로 유일해야 한다 — 같은 이름이 둘이면 "B은행으로 보내줘"가 하나로 정해지지 않고, 보낸 은행 이름을 믿을 수 없다."""
    key = normalized_name(name)
    stmt = select(Bank).where(func.replace(func.lower(Bank.name), " ", "") == key)
    for bank in (await db.scalars(stmt)).all():
        if normalized_name(bank.name) == key and bank.bank_code != exclude_code:
            raise ApiException(409, "BANK_NAME_TAKEN",
                               f"은행 이름 '{name}'은(는) 이미 쓰고 있습니다 ({bank.bank_code}). 다른 이름을 정하세요")


async def _create_bank(db: AsyncSession, bank_code: str, name: str, base_url: str, public_key: str,
                       net_debit_cap: int | None) -> tuple[Bank, str]:
    """은행 + 예치금 계좌(잔액 0) 생성. (bank, api_key)를 돌려준다. api_key는 이때만 볼 수 있다 (허브는 해시만 갖는다).
    순채무한도를 정하지 않으면 DEFAULT_NET_DEBIT_CAP (기본 10억): 등록 직후부터 보낼 수 있다 (SPEC 10.2)."""
    api_key = new_api_key()
    bank = Bank(
        bank_code=bank_code, name=name, base_url=base_url, api_key_hash=hash_api_key(api_key),
        net_debit_cap=get_settings().default_net_debit_cap if net_debit_cap is None else net_debit_cap,
        public_key=public_key, public_key_registered_at=datetime.now(UTC),
    )
    db.add(bank)
    await db.flush()  # reserve_accounts가 banks를 참조하므로 은행을 먼저 넣는다
    db.add(ReserveAccount(bank_code=bank.bank_code, balance=0))  # 예치금 계좌 (잔액 0, SETTLEMENT_PLAN 2절 5번)
    await db.flush()
    return bank, api_key


async def register_bank(db: AsyncSession, data: BankCreate) -> tuple[Bank, str]:
    """관리자 직접 등록 (코드를 관리자가 정한다. 시연용 990 등). 보통은 셀프 등록(self_register)을 쓴다."""
    if await get_bank(db, data.bank_code):
        raise ApiException(409, "BANK_EXISTS", f"은행 코드 {data.bank_code}는 이미 있습니다")
    await _require_unique_name(db, data.name)
    return await _create_bank(db, data.bank_code, data.name, _normalize_url(data.base_url), data.public_key,
                              data.net_debit_cap)


async def _require_bank(db: AsyncSession, bank_code: str) -> Bank:
    bank = await get_bank(db, bank_code)
    if bank is None:
        raise NotFound("BANK_NOT_FOUND", f"은행 코드 {bank_code}를 찾을 수 없습니다")
    return bank


async def update_bank(db: AsyncSession, bank_code: str, data: BankUpdate) -> Bank:
    """이름·base_url·순채무한도 변경. 진행 중인 재시도도 다음 호출부터 새 주소로 간다 (거래에는 주소를 저장하지 않는다)."""
    bank = await _require_bank(db, bank_code)
    if data.name is not None:
        await _require_unique_name(db, data.name, exclude_code=bank_code)
        bank.name = data.name
    if data.base_url is not None:
        bank.base_url = _normalize_url(data.base_url)
    if data.net_debit_cap is not None:
        bank.net_debit_cap = data.net_debit_cap
    await db.flush()
    return bank


async def update_me(db: AsyncSession, bank: Bank, name: str | None, base_url: str | None) -> Bank:
    """은행 셀프 변경 (PATCH /v1/me). base_url은 라우터가 ping으로 확인한 뒤에만 넘긴다. 이름은 유일해야 한다."""
    if name is not None:
        await _require_unique_name(db, name, exclude_code=bank.bank_code)
        bank.name = name
    if base_url is not None:
        bank.base_url = _normalize_url(base_url)
    await db.flush()
    return bank


async def rotate_keys(db: AsyncSession, bank_code: str) -> tuple[Bank, str]:
    """API 키만 새로 만든다 (유출 때). 공개키는 그대로다 (은행이 PUT /v1/keys로 바꾼다).
    API 키는 식별용이라 이것만으로는 은행 명의 요청을 만들 수 없다. 서명은 은행 개인키로만 된다."""
    bank = await _require_bank(db, bank_code)
    api_key = new_api_key()
    bank.api_key_hash = hash_api_key(api_key)
    await db.flush()
    return bank, api_key


async def set_active(db: AsyncSession, bank_code: str, active: bool) -> Bank:
    bank = await _require_bank(db, bank_code)
    bank.active = active
    await db.flush()
    return bank


# ---- 공개키 (SETTLEMENT_PLAN 12.2 5·8번, SPEC 2.6) ----

async def register_public_key(db: AsyncSession, bank: Bank, public_key: str) -> Bank:
    """공개키 등록·교체. 이미 다른 키가 있으면 그 키를 previous_public_key에 PREVIOUS_KEY_TTL_SECONDS(기본 24시간) 동안
    둔다 (진행 중 재시도가 깨지지 않게). 같은 키를 다시 넣으면 바꾸지 않는다."""
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
    """관리자가 대신 등록. 8c 전에 등록돼 공개키가 없는 은행(운영 901·902)을 살리는 길이기도 하다."""
    return await register_public_key(db, await _require_bank(db, bank_code), public_key)


# ---- 초대 코드·셀프 등록 (SETTLEMENT_PLAN 12.11, SPEC 3.5) ----

def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)  # SQLite는 시간대 없이 준다


def invite_status(invite: Invite, now: datetime | None = None) -> str:
    if invite.used_at is not None:
        return "USED"
    return "EXPIRED" if _aware(invite.expires_at) <= (now or datetime.now(UTC)) else "UNUSED"


async def create_invite(db: AsyncSession, note: str) -> Invite:
    invite = Invite(code=new_invite_code(), note=note,
                    expires_at=datetime.now(UTC) + timedelta(days=get_settings().invite_ttl_days))
    db.add(invite)
    await db.flush()
    return invite


async def list_invites(db: AsyncSession) -> list[Invite]:
    return list((await db.scalars(select(Invite).order_by(Invite.created_at.desc(), Invite.code))).all())


async def next_bank_code(db: AsyncSession) -> str:
    """BANK_CODE_START(기본 903)부터 비어 있는 가장 작은 코드. 관리자가 직접 넣은 코드(시연용 990 등)는 건너뛴다."""
    used = set((await db.scalars(select(Bank.bank_code))).all())
    for number in range(int(get_settings().bank_code_start), 1000):
        code = f"{number:03d}"
        if code not in used:
            return code
    raise ApiException(409, "BANK_CODE_EXHAUSTED", "배정할 수 있는 은행 코드가 없습니다. 허브 관리자에게 알려 주세요")


async def self_register(db: AsyncSession, data: BankRegister) -> tuple[Bank, str]:
    """초대 코드로 은행 등록 (인증 없음). 코드 확인 → 같은 base_url 확인 → 코드 배정 → 은행·예치금 계좌 → 초대 코드 사용 처리.

    초대 코드 행을 잠가(PostgreSQL FOR UPDATE) 같은 코드로 동시에 두 은행이 생기지 않게 한다. 은행 코드가 겹치는
    경합(다른 초대 코드로 동시에 등록)은 PK 충돌이 나고, 라우터가 새 트랜잭션으로 다시 한다."""
    invite = await db.scalar(select(Invite).where(Invite.code == data.invite_code).with_for_update())
    if invite is None or invite_status(invite) != "UNUSED":
        raise BadRequest("INVITE_INVALID", "초대 코드가 없거나 만료됐거나 이미 쓰였습니다. 허브 관리자에게 새 코드를 요청하세요")
    base_url = _normalize_url(data.base_url)
    existing = await db.scalar(select(Bank).where(Bank.base_url == base_url))
    if existing is not None:
        raise ApiException(409, "BANK_EXISTS", f"이 base_url로 이미 등록된 은행이 있습니다 ({existing.bank_code})")
    await _require_unique_name(db, data.name)  # 예외면 트랜잭션이 롤백돼 초대 코드는 쓰이지 않은 채로 남는다
    bank, api_key = await _create_bank(db, await next_bank_code(db), data.name, base_url, data.public_key, None)
    invite.used_at, invite.used_by_bank_code = datetime.now(UTC), bank.bank_code
    await db.flush()
    return bank, api_key
