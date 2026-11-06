"""요청 인증 의존성: 참가 은행(Ed25519 서명), 연동 지원용 은행 식별(API 키만, SPEC 3.8), 관리자(토큰)."""

import asyncio
import hmac
import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import get_db
from clearing_hub.core.exceptions import Unauthorized
from clearing_hub.core.signing import ALG_ED25519, ALG_HEADER, hash_api_key, verify_ed25519
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.support import failures

logger = logging.getLogger(__name__)


@dataclass
class VerifiedRequest:
    """검증을 통과한 요청의 원문 (이체 접수 증거 REQUEST_IN, SETTLEMENT_PLAN 12.3). request.state.verified에 둔다."""

    method: str
    path: str
    headers: dict[str, str]  # 서명 관련 헤더만 (X-Api-Key는 넣지 않는다)
    body: bytes
    signature_alg: str


async def current_bank(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_bank_code: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_timestamp: Annotated[str | None, Header()] = None,
    x_signature: Annotated[str | None, Header()] = None,
    x_signature_alg: Annotated[str | None, Header()] = None,
) -> Bank:
    """X-Bank-Code·X-Api-Key로 은행을 찾고(식별), 등록된 Ed25519 공개키로 서명을 검증한다(인증, SPEC 2).

    공개키는 현재 키와, 교체 뒤 24시간 안의 이전 키다. `X-Signature-Alg`는 `ed25519`이거나 없어야 한다 (없으면 ed25519).
    허브는 은행의 비밀을 갖지 않으므로, 허브 DB가 통째로 새도 이 검증을 통과하는 요청은 만들 수 없다.

    401을 낼 때마다 `auth_failures`에 한 건 남긴다 (연동 지원, SPEC 3.8). API 키가 맞은 은행이면 그 코드로 남겨
    은행이 `GET /v1/diagnose/failures`로 스스로 보고, 키가 틀리면 은행을 모르므로 bank_code 없이 남긴다.
    API 키가 맞은 은행이면 401 본문에 규칙 진단 1순위 가설 한 줄(`hint`, SPEC 5절)도 넣는다. 키가 틀린 요청에는 넣지
    않는다 (외부 스캐너에 서명 규칙을 알려 주지 않는다)."""
    identified: list[Bank] = []  # API 키가 맞은 은행 (아래 _authenticate가 넣는다)
    try:
        return await _authenticate(request, db, identified, x_bank_code, x_api_key, x_timestamp, x_signature,
                                   x_signature_alg)
    except Unauthorized as exc:
        bank = identified[0] if identified else None
        failure = await failures.record_bank_to_hub(request, exc.code, exc.message, bank.bank_code if bank else None)
        if bank is not None and failure is not None:
            hint = await _hint(bank, failure)
            if hint:
                exc.extra["hint"] = hint
        raise


HINT_TIMEOUT_SECONDS = 1


async def _hint(bank: Bank, failure) -> str | None:
    """401 본문의 hint: 진단기 1순위 가설 한 줄. 진단이 느리거나(1초) 실패하면 None (401은 그대로 나간다)."""
    from clearing_hub.domains.support.service import auth_hint  # support.service가 bank_client를 import하므로 늦게

    try:
        return await asyncio.wait_for(asyncio.to_thread(auth_hint, bank, failure), HINT_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("401 hint를 만들지 못했습니다: %s", type(exc).__name__)
        return None


async def _authenticate(
    request: Request, db: AsyncSession, identified: list[Bank], x_bank_code: str | None, x_api_key: str | None,
    x_timestamp: str | None, x_signature: str | None, x_signature_alg: str | None,
) -> Bank:
    if not (x_bank_code and x_api_key and x_timestamp and x_signature):
        raise Unauthorized(message="인증 헤더가 없습니다")
    bank = await bank_service.get_bank(db, x_bank_code)
    if bank is None or not hmac.compare_digest(bank.api_key_hash, hash_api_key(x_api_key)):
        raise Unauthorized(message="은행 코드 또는 API 키가 맞지 않습니다")
    identified.append(bank)
    if not bank.active:
        raise Unauthorized(code="BANK_INACTIVE", message="비활성 은행입니다")
    alg = (x_signature_alg or ALG_ED25519).strip().lower()
    if alg != ALG_ED25519:
        raise Unauthorized(code="SIGNATURE_INVALID", message=f"지원하지 않는 서명 방식입니다 ({ALG_HEADER}: ed25519만)")
    keys = bank.verification_keys()
    if not keys:
        raise Unauthorized(code="SIGNATURE_INVALID",
                           message="등록된 공개키가 없습니다. 허브 관리자에게 공개키 등록을 요청하세요 (SPEC 2.6)")
    body = await request.body()
    skew = get_settings().signature_max_skew_seconds
    # 허브 API의 규격 경로는 `/v1/...` 그대로다 (SPEC 2.2)
    path = request.url.path
    errors = [verify_ed25519(key, x_timestamp, request.method, path, body, x_signature, skew) for key in keys]
    if None not in errors:
        error = errors[0]
        raise Unauthorized(code=error, message="서명이 맞지 않습니다" if error == "SIGNATURE_INVALID" else "서명 시각이 만료됐습니다")
    headers = {"X-Bank-Code": x_bank_code, "X-Timestamp": x_timestamp, "X-Signature": x_signature}
    if x_signature_alg:
        headers[ALG_HEADER] = x_signature_alg
    request.state.verified = VerifiedRequest(request.method, path, headers, body, alg)
    return bank


async def identified_bank(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_bank_code: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Bank:
    """서명 없이 X-Bank-Code·X-Api-Key만으로 은행을 찾는다 (연동 지원 API, SPEC 3.8). 서명이 안 되는 상태에서 원인을
    찾으려고 부르는 읽기 전용 API에만 쓴다 (돈·등록 정보를 바꾸는 API에는 쓰지 않는다). 비활성 은행도 자기 실패는 본다.
    이 의존성의 401은 auth_failures에 남기지 않는다 (서명 검증을 하지 않았으므로 진단할 것이 없다)."""
    if not (x_bank_code and x_api_key):
        raise Unauthorized(message="X-Bank-Code와 X-Api-Key가 필요합니다")
    bank = await bank_service.get_bank(db, x_bank_code)
    if bank is None or not hmac.compare_digest(bank.api_key_hash, hash_api_key(x_api_key)):
        raise Unauthorized(message="은행 코드 또는 API 키가 맞지 않습니다")
    return bank


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().admin_token.get_secret_value()
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise Unauthorized(message="관리자 토큰이 맞지 않습니다")
