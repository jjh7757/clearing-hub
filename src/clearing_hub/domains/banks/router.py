import logging
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.auth import current_bank, require_admin
from clearing_hub.core import alerts
from clearing_hub.core.bank_client import BankReply, bank_client, observed
from clearing_hub.core.config import get_settings
from clearing_hub.core.database import get_db
from clearing_hub.core.exceptions import ApiException
from clearing_hub.core.signing import fingerprint, public_key_of
from clearing_hub.domains.banks import service
from clearing_hub.domains.banks.models import Bank, Invite
from clearing_hub.domains.support import service as support_service
from clearing_hub.domains.banks.schemas import (
    BankAdmin, BankCreate, BankCredentials, BankList, BankMe, BankPublic, BankRegister, BankRegistered, BankUpdate, HubKey,
    InviteAdmin, InviteCreate, InviteIssued, KeyStatus, MeUpdate, PingResult, PublicKeyIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/banks", tags=["banks"])
keys_router = APIRouter(prefix="/v1", tags=["keys"])  # 부인방지 키·셀프 서비스 (SETTLEMENT_PLAN 12.5·12.11, SPEC 2.6·3.6)
admin_router = APIRouter(prefix="/admin/banks", tags=["admin"], dependencies=[Depends(require_admin)])
invite_admin_router = APIRouter(prefix="/admin/invites", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[AsyncSession, Depends(get_db)]
PING_ACCOUNT = "000-000-000000"  # 연결 확인용 없는 계좌 (SPEC 3.6)


# ---- 연결 확인 (ping) ----

async def ping_bank(bank: Bank, base_url: str | None = None) -> PingResult:
    return (await _ping(bank, base_url))[0]


async def _ping(bank: Bank, base_url: str | None = None) -> tuple[PingResult, BankReply]:
    """{base_url}/inquiry를 없는 계좌로 불러 본다 (SPEC 3.6). 허브 키로 서명하고, 은행의 응답 서명을 검증한다.

    정상(ok)은 응답 서명이 맞고 status가 200 또는 404 ACCOUNT_NOT_FOUND일 때다. 응답 서명이 틀리면 BankReply는
    "응답 없음"이 되므로, 실제로 받은 상태·본문은 오간 원문(exchange)에서 꺼내 보여 준다 (은행이 고칠 곳을 찾게)."""
    reply = await bank_client.inquiry(bank, PING_ACCOUNT, base_url=base_url)
    status, body = observed(reply)
    ok = reply.signature_valid is True and (
        status == 200 or (status == 404 and body.get("error") == "ACCOUNT_NOT_FOUND"))
    return PingResult(ok=ok, reachable=status is not None, status=status, body=body,
                      response_signature_valid=reply.signature_valid), reply


def _hint(bank: Bank, reply: BankReply) -> str:
    """알림 둘째 문장: 규칙 진단 1순위 가설 한 줄 (LLM 없음, SETTLEMENT_PLAN 14절). 없으면 빈 문자열."""
    hint = support_service.reply_hint(bank, reply)
    return f"\n{hint}" if hint else ""


def _hub_public_key() -> str:
    return public_key_of(get_settings().hub_signing_key.get_secret_value())


# ---- 은행용 (SPEC 3.1·3.5) ----

@router.get("", response_model=BankList)
async def list_banks(db: DB, _: Annotated[Bank, Depends(current_bank)]):
    """참가 은행 목록 (SPEC 3.1). 화면의 '받는 은행' 선택용."""
    return BankList(banks=[BankPublic.model_validate(b, from_attributes=True) for b in await service.list_banks(db)])


@router.post("/register", response_model=BankRegistered, status_code=201)
async def register(data: BankRegister, db: DB):
    """셀프 등록 (SPEC 3.5, 인증 없음 — 초대 코드가 인증이다). 은행 코드는 허브가 배정한다.

    등록을 커밋한 뒤 새 주소로 ping해 결과를 함께 준다. ping이 실패해도 등록은 그대로이고(active), 은행이 고친 뒤
    POST /v1/me/ping으로 다시 확인한다. api_key는 이 응답에만 있다."""
    for attempt in range(3):
        try:
            async with db.begin():
                bank, api_key = await service.self_register(db, data)
            break
        except IntegrityError:  # 다른 등록과 같은 은행 코드를 동시에 집음 → 새 트랜잭션에서 다음 빈 코드로
            if attempt == 2:
                raise
            logger.warning("셀프 등록 은행 코드 경합, 다시 시도 (%s)", attempt + 1)
    logger.info("셀프 등록: %s %s", bank.bank_code, bank.name, extra={"bank_code": bank.bank_code})
    hub_public = _hub_public_key()
    ping, reply = await _ping(bank)
    alerts.notify("register", f"새 은행이 셀프 등록했습니다: {bank.bank_code} {bank.name}, "
                  f"ping {'정상' if ping.ok else f'실패 (HTTP {ping.status}, 응답 서명 {ping.response_signature_valid})'}"
                  + ("" if ping.ok else _hint(bank, reply)),
                  bank_code=bank.bank_code, base_url=bank.base_url)
    return BankRegistered(bank_code=bank.bank_code, name=bank.name, api_key=api_key, hub_public_key=hub_public,
                          hub_fingerprint=fingerprint(hub_public), net_debit_cap=bank.net_debit_cap, ping=ping)


# ---- 은행 셀프 서비스 /v1/me (SPEC 3.6) ----

def _me(bank: Bank) -> BankMe:
    return BankMe.model_validate(bank, from_attributes=True)


@keys_router.get("/me", response_model=BankMe)
async def get_me(bank: Annotated[Bank, Depends(current_bank)]):
    """내 등록 정보: 코드·이름·base_url·활성·공개키 지문·순채무한도."""
    return _me(bank)


@keys_router.patch("/me", response_model=BankMe)
async def patch_me(data: MeUpdate, db: DB, bank: Annotated[Bank, Depends(current_bank)]):
    """이름·base_url 변경. base_url은 새 주소에 ping해 정상(200 또는 404 ACCOUNT_NOT_FOUND, 응답 서명 맞음)일 때만 바꾼다.
    아니면 400 PING_FAILED와 ping 결과 (아무것도 바꾸지 않는다). 진행 중인 재시도도 다음 호출부터 새 주소로 간다."""
    new_url = str(data.base_url).rstrip("/") if data.base_url is not None else None
    if new_url is not None and new_url != bank.base_url:
        ping, reply = await _ping(bank, base_url=new_url)
        if not ping.ok:
            alerts.notify("ping_failed", f"은행 {bank.bank_code}의 base_url 변경이 ping 실패로 거절됐습니다 "
                          f"(HTTP {ping.status}, 응답 서명 {ping.response_signature_valid})" + _hint(bank, reply),
                          bank_code=bank.bank_code, base_url=new_url)
            raise ApiException(400, "PING_FAILED", "새 base_url이 정상으로 응답하지 않아 바꾸지 않았습니다 (ping 확인)",
                               extra={"ping": ping.model_dump()})
    # current_bank가 이미 세션을 썼으므로(autobegin) begin() 대신 commit한다
    bank = await service.update_me(db, bank, data.name, new_url)
    await db.commit()
    return _me(bank)


@keys_router.post("/me/ping", response_model=PingResult)
async def ping_me(bank: Annotated[Bank, Depends(current_bank)]):
    """지금 등록된 base_url로 ping (SPEC 3.6). 받는 쪽 3개 API·응답 서명을 고친 뒤 확인하는 용도."""
    return await ping_bank(bank)


# ---- 부인방지 키 (SETTLEMENT_PLAN 12.5, SPEC 2.6) ----

def _key_status(bank: Bank) -> KeyStatus:
    return KeyStatus(
        bank_code=bank.bank_code, fingerprint=bank.public_key_fingerprint, registered_at=bank.public_key_registered_at,
        previous_fingerprint=fingerprint(bank.previous_public_key) if bank.previous_public_key else None,
        previous_key_expires_at=bank.previous_key_expires_at, hub_fingerprint=fingerprint(_hub_public_key()),
    )


@keys_router.get("/hub-key", response_model=HubKey)
async def hub_key():
    """허브 Ed25519 공개키 (인증 불필요). 처음 받을 때 허브 관리자가 따로 알려 준 지문과 맞춰 본다 (SPEC 2.6)."""
    public_key = _hub_public_key()
    return HubKey(public_key=public_key, fingerprint=fingerprint(public_key))


@keys_router.put("/keys", response_model=KeyStatus)
async def put_my_key(data: PublicKeyIn, db: DB, bank: Annotated[Bank, Depends(current_bank)]):
    """내 Ed25519 공개키 교체 (SPEC 2.6). 지금(옛) 키로 서명한 요청이라 자기 것만 바꾼다. 이전 키는 24시간 더 받는다."""
    # current_bank가 이미 세션을 썼으므로(autobegin) begin() 대신 commit한다
    bank = await service.register_public_key(db, bank, data.public_key)
    await db.commit()
    return _key_status(bank)


@keys_router.get("/keys", response_model=KeyStatus)
async def get_my_key(bank: Annotated[Bank, Depends(current_bank)]):
    """내 키 등록 상태: 지문, 이전 키 만료 시각, 허브 공개키 지문."""
    return _key_status(bank)


# ---- 관리자 ----

@admin_router.get("", response_model=list[BankAdmin])
async def admin_list_banks(db: DB):
    return [BankAdmin.model_validate(b, from_attributes=True) for b in await service.list_banks(db, active_only=False)]


@admin_router.post("", response_model=BankCredentials, status_code=201)
async def admin_register_bank(data: BankCreate, db: DB):
    """관리자 직접 등록 (코드를 정해서. 시연용 990 등). 공개키 필수, 한도를 빼면 DEFAULT_NET_DEBIT_CAP.
    api_key는 이때만 보여 준다. 보통은 초대 코드(POST /admin/invites)로 은행이 셀프 등록한다."""
    async with db.begin():
        bank, api_key = await service.register_bank(db, data)
    return BankCredentials(**BankAdmin.model_validate(bank, from_attributes=True).model_dump(), api_key=api_key)


@admin_router.patch("/{bank_code}", response_model=BankAdmin)
async def admin_update_bank(bank_code: str, data: BankUpdate, db: DB):
    """이름·base_url·순채무한도 변경. 주소를 바꿨으면 /ping으로 확인한다."""
    async with db.begin():
        bank = await service.update_bank(db, bank_code, data)
    return BankAdmin.model_validate(bank, from_attributes=True)


@admin_router.post("/{bank_code}/keys", response_model=BankCredentials)
async def admin_rotate_keys(bank_code: str, db: DB):
    """API 키 재발급 (유출 때). 공개키는 바꾸지 않는다 (은행이 PUT /v1/keys로, 또는 관리자가 /public-key로)."""
    async with db.begin():
        bank, api_key = await service.rotate_keys(db, bank_code)
    return BankCredentials(**BankAdmin.model_validate(bank, from_attributes=True).model_dump(), api_key=api_key)


@admin_router.patch("/{bank_code}/active", response_model=BankAdmin)
async def admin_set_active(bank_code: str, active: bool, db: DB):
    async with db.begin():
        bank = await service.set_active(db, bank_code, active)
    return BankAdmin.model_validate(bank, from_attributes=True)


@admin_router.post("/{bank_code}/ping")
async def admin_ping(bank_code: str, db: DB):
    """연결 상태 확인 (SPEC 3.6). POST /v1/me/ping과 같은 결과에 bank_code를 붙인다."""
    bank = await service.require_active_bank(db, bank_code)
    return {"bank_code": bank_code, **(await ping_bank(bank)).model_dump()}


@admin_router.post("/{bank_code}/public-key", response_model=BankAdmin)
async def admin_register_public_key(bank_code: str, data: PublicKeyIn, db: DB):
    """은행이 준 공개키를 관리자가 대신 등록한다 (8c 전 은행의 첫 등록, 은행이 키를 잃어버렸을 때). 이전 키는 24시간 받는다."""
    async with db.begin():
        bank = await service.admin_register_public_key(db, bank_code, data.public_key)
    return BankAdmin.model_validate(bank, from_attributes=True)


# ---- 초대 코드 (SETTLEMENT_PLAN 12.11, docs/ONBOARDING.md) ----

def _invite_admin(invite: Invite) -> InviteAdmin:
    return InviteAdmin(code_prefix=invite.code[:8], note=invite.note, created_at=invite.created_at,
                       expires_at=invite.expires_at, used_at=invite.used_at, used_by_bank_code=invite.used_by_bank_code,
                       status=service.invite_status(invite))


def invite_message(code: str, expires_at: datetime, hub_url: str) -> str:
    """관리자가 그대로 복사해 팀 담당자에게 DM으로 보낼 초대 문구 (docs/ONBOARDING.md 1)."""
    expires = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)  # SQLite는 시간대 없이 준다
    local = expires.astimezone(ZoneInfo(get_settings().settlement_timezone)).strftime("%Y-%m-%d %H:%M")
    hub_public = _hub_public_key()
    lines = [
        "ClearingHub 참가 초대입니다.",
        f"- 초대 코드: {code}",
        f"- 만료: {local} (KST, {expires.strftime('%Y-%m-%dT%H:%M:%SZ')})",
        f"- 허브 주소: {hub_url}",
        "- 연동 규격: ClearingHub 저장소의 docs/SPEC.md (7절 연동 절차부터)",
        f"- 허브 공개키: {hub_public}",
        f"- 허브 공개키 지문: {fingerprint(hub_public)} (등록 응답의 hub_fingerprint가 이 값과 같은지 꼭 확인하세요)",
        f"- 등록 방법: 키 쌍을 만들고 받는 쪽 API를 띄운 뒤 POST {hub_url}/v1/banks/register 에 "
        "invite_code·name·base_url·public_key를 보냅니다 (SPEC 3.5)",
        "초대 코드는 한 번만 쓸 수 있습니다. 공개 채널(GitHub·단체 채팅방 등)에 올리지 마세요.",
    ]
    return "\n".join(lines)


@invite_admin_router.post("", response_model=InviteIssued, status_code=201)
async def admin_create_invite(data: InviteCreate, db: DB, request: Request):
    """초대 코드 발급 (기본 7일, INVITE_TTL_DAYS). code는 이때만 보여 준다.
    message(초대 문구: 코드·만료·허브 주소·허브 공개키·지문·등록 방법)를 그대로 팀 담당자에게 개별로 보낸다."""
    async with db.begin():
        invite = await service.create_invite(db, data.note)
    hub_url = (get_settings().hub_public_url or str(request.base_url)).rstrip("/")
    return InviteIssued(code=invite.code, note=invite.note, expires_at=invite.expires_at,
                        message=invite_message(invite.code, invite.expires_at, hub_url))


@invite_admin_router.get("", response_model=list[InviteAdmin])
async def admin_list_invites(db: DB):
    """초대 코드 목록: 사용 여부(UNUSED·USED·EXPIRED), 쓴 은행 코드. 코드는 앞 8자만."""
    return [_invite_admin(i) for i in await service.list_invites(db)]
