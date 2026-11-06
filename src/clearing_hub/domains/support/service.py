"""연동 진단기 (SETTLEMENT_PLAN 14절, SPEC 3.8). 규칙만 쓴다.

그 은행의 최근 인증 실패(양방향, `auth_failures`)를 규칙 진단기(core/signature_doctor.py)로 진단해 돌려주고
(`GET /v1/diagnose/failures`, 관리자 `GET /admin/banks/{code}/auth-failures`), 401 본문의 `hint`(core/auth)와
알림의 진단 한 줄(ping 실패·셀프 등록·셀프 시험)을 만든다.

은행 API는 자기 은행으로 식별된 실패만 본다. 관리자 API는 API 키가 틀려 식별되지 않았지만 X-Bank-Code가 그 은행이었던 것도 본다.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.bank_client import BankReply, observed
from clearing_hub.core.config import get_settings
from clearing_hub.core.signature_doctor import DoctorContext, diagnose
from clearing_hub.core.signing import fingerprint, public_key_of
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.support import failures
from clearing_hub.domains.support.models import AuthFailure
from clearing_hub.domains.support.schemas import FailureOut, Finding, HypothesisOut
from clearing_hub.domains.transfers.schemas import spec_time

logger = logging.getLogger(__name__)

LIST_FAILURES = 20  # GET /v1/diagnose/failures
PREVIEW = 500


def _hub_public_key() -> str:
    return public_key_of(get_settings().hub_signing_key.get_secret_value())


def context_for(bank: Bank | None, claimed_bank_exists: bool | None = None) -> DoctorContext:
    settings = get_settings()
    ctx = DoctorContext(hub_public_url=settings.hub_public_url, hub_fingerprint=fingerprint(_hub_public_key()),
                        max_skew=settings.signature_max_skew_seconds, claimed_bank_exists=claimed_bank_exists)
    if bank is not None:
        valid = bank.verification_keys()
        ctx.current_key, ctx.previous_key = bank.public_key, bank.previous_public_key
        ctx.previous_valid = bool(bank.previous_public_key) and bank.previous_public_key in valid
        ctx.base_url = bank.base_url
    return ctx


def _preview(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    return text if len(text) <= PREVIEW else text[:PREVIEW] + "…"


def finding(failure: AuthFailure, ctx: DoctorContext) -> Finding:
    result = diagnose(failure, ctx)
    out = FailureOut(
        failure_id=failure.failure_id, direction=failure.direction, reason=failure.reason, message=failure.message,
        method=failure.method, path=failure.path, headers=failure.headers_json or {}, body_preview=_preview(failure.body) or "",
        body_length=len(failure.body or b""), http_status=failure.http_status, response_headers=failure.response_headers_json,
        response_body_preview=_preview(failure.response_body), created_at=spec_time(failure.created_at) or "",
    )
    return Finding(failure=out, hypotheses=[HypothesisOut(**h.__dict__) for h in result.hypotheses])


async def failure_findings(db: AsyncSession, bank: Bank, limit: int, include_claimed: bool = False) -> list[Finding]:
    """그 은행의 최근 실패와 규칙 진단 (새것부터)."""
    rows = await failures.recent(db, bank.bank_code, limit, include_claimed)
    ctx = context_for(bank)
    claimed_ctx = context_for(bank, claimed_bank_exists=True)  # 관리자용: X-Bank-Code는 있는 은행인데 API 키가 틀림
    return [finding(row, claimed_ctx if row.bank_code is None else ctx) for row in rows]


# ---- 401 본문의 hint (core/auth) ----

HINT_MAX = 500


def auth_hint(bank: Bank, failure: AuthFailure) -> str | None:
    """API 키가 맞은 은행의 401에 넣을 한 줄: 1순위 가설(시계 차이·실제 값은 detail에) → 고칠 곳 (SPEC 절) + 자세히 보는 곳.
    동기 함수다 (core/auth가 스레드에서 1초 안에 부른다)."""
    top = diagnose(failure, context_for(bank)).top
    if top is None or top.code == "UNKNOWN_REASON":
        return None
    mark = "확인됨" if top.verified else "추정"
    text = f"원인 후보({mark}): {top.title}. {top.detail} → {top.fix} (SPEC {top.spec_ref}) 자세히: GET /v1/diagnose/failures"
    return text if len(text) <= HINT_MAX else text[:HINT_MAX - 1] + "…"


# ---- 알림에 붙이는 한 줄 (core/alerts) ----

def reply_hint(bank: Bank, reply: BankReply) -> str | None:
    """허브 → 은행 호출이 정상이 아닐 때 규칙 진단 1순위 가설 한 줄 (SPEC 3.6 ping 결과 표와 같은 구분). 정상이면 None."""
    try:
        failure = failures.failure_from_reply(bank, reply)
        if failure is not None:
            return diagnose(failure, context_for(bank)).one_line()
        status, body = observed(reply)
    except Exception as exc:  # 알림 문구 때문에 호출한 쪽이 실패하면 안 된다
        logger.warning("진단 한 줄을 만들지 못했습니다: %s", type(exc).__name__)
        return None
    if status is None:
        return "원인 후보(추정): 연결 안 됨 → base_url 주소·방화벽·공인 인증서·5초 타임아웃 확인 (SPEC 3.6)"
    if status >= 500:
        return f"원인 후보(추정): 은행 서버 오류 HTTP {status} → 은행 서버 로그 확인 (SPEC 3.6)"
    if status == 404 and body.get("error") != "ACCOUNT_NOT_FOUND":
        return "원인 후보(추정): base_url 경로가 틀림 → 4절 API가 base_url 바로 아래에 있는지 확인 (SPEC 3.6)"
    return None

