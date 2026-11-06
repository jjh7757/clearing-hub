"""요청 인증 의존성: 참가 은행(Ed25519 서명), 관리자(토큰)."""

import hmac
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
    허브는 은행의 비밀을 갖지 않으므로, 허브 DB가 통째로 새도 이 검증을 통과하는 요청은 만들 수 없다."""
    if not (x_bank_code and x_api_key and x_timestamp and x_signature):
        raise Unauthorized(message="인증 헤더가 없습니다")
    bank = await bank_service.get_bank(db, x_bank_code)
    if bank is None or not hmac.compare_digest(bank.api_key_hash, hash_api_key(x_api_key)):
        raise Unauthorized(message="은행 코드 또는 API 키가 맞지 않습니다")
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


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().admin_token.get_secret_value()
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise Unauthorized(message="관리자 토큰이 맞지 않습니다")
