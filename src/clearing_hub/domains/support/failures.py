"""인증 실패 기록 (SETTLEMENT_PLAN 14절, `auth_failures`). 규칙 진단기(core/signature_doctor.py)의 재료다.

남기는 것
- BANK_TO_HUB: `core/auth.current_bank`가 401을 낼 때마다 (헤더 없음·API 키 불일치·비활성·공개키 없음·서명 불일치·만료)
- HUB_TO_BANK: 허브가 은행을 부른 응답이 401·403이거나 응답 서명이 없거나 틀릴 때 (`core/bank_client`의 모든 호출:
  ping·수취 조회·입금·입금 확인·계좌 목록). 우리가 보낸 헤더·경로·본문과 은행이 돌려준 상태·헤더·본문

**기록은 부가 기능이다.** 기록이 실패하거나 느려도 401 응답·이체가 막히면 안 된다. 그래서 요청과 다른 세션으로 쓰고,
예외는 로그만 남긴다. BANK_TO_HUB는 3초 안에서 기다린다 (요청 세션은 아직 쓰기 잠금을 잡지 않았다). HUB_TO_BANK는
호출한 쪽이 트랜잭션 안일 수 있어(SQLite는 쓰기 잠금이 하나) `create_task`로 띄우고 기다리지 않는다 (`drain`은 테스트·종료용).

은행당 최근 KEEP_PER_BANK건만 남긴다 (기록할 때 오래된 것을 지운다). 은행을 식별하지 못한 실패(API 키가 틀림)는
`bank_code` NULL로 KEEP_UNIDENTIFIED건까지. **X-Api-Key 값은 저장하지 않는다** (보냈는지만).
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.support.models import AuthFailure, FailureDirection

if TYPE_CHECKING:  # bank_client가 이 모듈을 import하므로 실행 때는 import하지 않는다
    from clearing_hub.core.bank_client import BankReply
    from clearing_hub.domains.banks.models import Bank

logger = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
KEEP_PER_BANK = 200
KEEP_UNIDENTIFIED = 1000
RECORD_TIMEOUT_SECONDS = 3
# 남기는 요청 헤더 (서명 검증에 필요한 것과 추적 ID). X-Api-Key는 넣지 않는다
REQUEST_HEADERS = ("X-Bank-Code", "X-Timestamp", "X-Signature", "X-Signature-Alg", "X-Request-ID")

_pending: set[asyncio.Task] = set()


def _clip(body: bytes) -> tuple[bytes, bool]:
    return (body[:MAX_BODY], True) if len(body) > MAX_BODY else (body, False)


def _now() -> datetime:
    return datetime.now(UTC)


async def _trim(db: AsyncSession, bank_code: str | None) -> None:
    keep = KEEP_PER_BANK if bank_code else KEEP_UNIDENTIFIED
    same = AuthFailure.bank_code == bank_code if bank_code else AuthFailure.bank_code.is_(None)
    cutoff = await db.scalar(select(AuthFailure.failure_id).where(same)
                             .order_by(AuthFailure.failure_id.desc()).offset(keep - 1).limit(1))
    if cutoff is not None:
        await db.execute(delete(AuthFailure).where(same, AuthFailure.failure_id < cutoff))


async def _save(failure: AuthFailure) -> None:
    try:
        async with SessionLocal() as db:
            async with db.begin():
                db.add(failure)
                await db.flush()
                await _trim(db, failure.bank_code)
    except Exception as exc:  # 기록 실패가 호출한 쪽으로 번지지 않게 모두 삼킨다
        logger.warning("인증 실패 기록 실패 (%s %s): %s", failure.direction, failure.reason, type(exc).__name__)


# ---- BANK_TO_HUB ----

async def record_bank_to_hub(request: Request, reason: str, message: str, bank_code: str | None) -> None:
    """current_bank가 401을 낼 때. bank_code는 API 키가 맞은 은행이면 그 코드, 아니면 None. 예외를 올리지 않는다."""
    try:
        body, truncated = _clip(await request.body())
        headers = {name: request.headers[name] for name in REQUEST_HEADERS if name in request.headers}
        failure = AuthFailure(
            direction=FailureDirection.BANK_TO_HUB, claimed_bank_code=(request.headers.get("X-Bank-Code") or None),
            bank_code=bank_code, reason=reason, message=message[:300], method=request.method,
            path=request.url.path[:300], query=(request.url.query or None), url=str(request.url.replace(query=""))[:500],
            headers_json=headers, api_key_sent=bool(request.headers.get("X-Api-Key")), body=body, body_truncated=truncated,
            created_at=_now(),
        )
        if failure.claimed_bank_code:
            failure.claimed_bank_code = failure.claimed_bank_code[:20]
        await asyncio.wait_for(_save(failure), RECORD_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("인증 실패 기록 실패 (BANK_TO_HUB %s): %s", reason, type(exc).__name__)


# ---- HUB_TO_BANK ----

def _json_dict(raw: bytes | None) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def failure_from_reply(bank: "Bank", reply: "BankReply") -> AuthFailure | None:
    """은행 응답이 인증 실패(401·403, 응답 서명 없음·틀림)면 저장하지 않은 AuthFailure, 아니면 None.
    알림(ping 실패 등)에 진단 한 줄을 붙일 때도 이것을 저장하지 않고 진단기에 넘긴다."""
    ex = reply.exchange
    if ex is None or ex.response_status is None:
        return None
    status = ex.response_status
    data = _json_dict(ex.response_body)
    if status in (401, 403):
        code = data.get("error")
        reason = code if isinstance(code, str) and code else f"HTTP_{status}"
    elif reply.signature_valid is False:
        reason = "RESPONSE_SIGNATURE_INVALID"
    else:
        return None
    message = data.get("message")
    body, truncated = _clip(ex.request_body)
    response_body, _ = _clip(ex.response_body)
    return AuthFailure(
        direction=FailureDirection.HUB_TO_BANK, claimed_bank_code=None, bank_code=bank.bank_code, reason=reason[:40],
        message=str(message)[:300] if message else None, method=ex.method, path=ex.path[:300], query=None,
        url=(ex.url or "")[:500] or None, headers_json=dict(ex.request_headers), api_key_sent=None, body=body,
        body_truncated=truncated, http_status=status, response_headers_json=dict(ex.response_headers),
        response_body=response_body, response_signature_valid=reply.signature_valid, created_at=_now(),
    )


def record_reply(bank: "Bank", reply: "BankReply") -> asyncio.Task | None:
    """bank_client가 응답마다 부른다. 인증 실패면 기록을 띄우고 바로 돌아간다 (기다리지 않는다)."""
    try:
        failure = failure_from_reply(bank, reply)
        if failure is None:
            return None
        task = asyncio.get_running_loop().create_task(_save(failure))
    except Exception as exc:  # 루프 밖이거나 응답 모양이 이상해도 호출한 쪽은 그대로
        logger.warning("인증 실패 기록 실패 (HUB_TO_BANK): %s", type(exc).__name__)
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def drain(timeout: float = 5) -> None:
    """아직 쓰는 중인 기록을 기다린다 (테스트, 앱 종료)."""
    if _pending:
        await asyncio.wait(list(_pending), timeout=timeout)


# ---- 조회 ----

async def recent(db: AsyncSession, bank_code: str, limit: int, include_claimed: bool = False) -> list[AuthFailure]:
    """그 은행의 최근 실패(새것부터). include_claimed는 관리자용: API 키가 틀려 식별되지 않았지만 X-Bank-Code가
    그 코드였던 실패도 넣는다 (누구나 그 코드를 헤더에 넣을 수 있으므로 은행에게는 보여 주지 않는다)."""
    cond = AuthFailure.bank_code == bank_code
    if include_claimed:
        cond = or_(cond, and_(AuthFailure.bank_code.is_(None), AuthFailure.claimed_bank_code == bank_code))
    stmt = select(AuthFailure).where(cond).order_by(AuthFailure.failure_id.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())
