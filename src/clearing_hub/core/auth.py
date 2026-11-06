"""요청 인증 의존성: 참가 은행(서명), 관리자(토큰)."""

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import get_db
from clearing_hub.core.exceptions import Unauthorized
from clearing_hub.core.signing import ALG_ED25519, ALG_HEADER, ALG_HMAC, hash_api_key, verify, verify_ed25519
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank, SignatureMode

# 모드별로 받는 알고리즘 (SETTLEMENT_PLAN 12.2 7번, SPEC 2.6)
ALLOWED_ALGS = {
    SignatureMode.HMAC: {ALG_HMAC},
    SignatureMode.BOTH: {ALG_HMAC, ALG_ED25519},
    SignatureMode.ED25519: {ALG_ED25519},
}
_MODE_HINT = {
    SignatureMode.HMAC: "이 은행은 hmac 모드입니다. 공개키를 등록(PUT /v1/keys)하고 허브 관리자에게 both 전환을 요청하세요",
    SignatureMode.ED25519: "이 은행은 ed25519 모드입니다. Ed25519로 서명하고 X-Signature-Alg: ed25519를 넣으세요",
}


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
    """X-Bank-Code·X-Api-Key로 은행을 찾고 서명을 검증한다 (SPEC 2).

    `X-Signature-Alg: ed25519`면 등록된 공개키(현재, 또는 교체 뒤 24시간 안의 이전 키)로, 없거나 `hmac`이면 secret으로
    검증한다. 은행의 signature_mode가 받지 않는 알고리즘이면 401 SIGNATURE_INVALID (message에 모드 안내)."""
    if not (x_bank_code and x_api_key and x_timestamp and x_signature):
        raise Unauthorized(message="인증 헤더가 없습니다")
    bank = await bank_service.get_bank(db, x_bank_code)
    if bank is None or not hmac.compare_digest(bank.api_key_hash, hash_api_key(x_api_key)):
        raise Unauthorized(message="은행 코드 또는 API 키가 맞지 않습니다")
    if not bank.active:
        raise Unauthorized(code="BANK_INACTIVE", message="비활성 은행입니다")
    alg = (x_signature_alg or ALG_HMAC).strip().lower()
    if alg not in (ALG_HMAC, ALG_ED25519):
        raise Unauthorized(code="SIGNATURE_INVALID", message=f"지원하지 않는 서명 방식입니다 ({ALG_HEADER}: hmac 또는 ed25519)")
    mode = SignatureMode(bank.signature_mode)
    if alg not in ALLOWED_ALGS[mode]:
        raise Unauthorized(code="SIGNATURE_INVALID", message=_MODE_HINT[mode])
    body = await request.body()
    skew = get_settings().signature_max_skew_seconds
    # 허브 API의 규격 경로는 `/v1/...` 그대로다 (SPEC 2.2)
    path = request.url.path
    if alg == ALG_ED25519:
        keys = bank.verification_keys()
        if not keys:
            raise Unauthorized(code="SIGNATURE_INVALID", message="등록된 공개키가 없습니다. PUT /v1/keys로 먼저 등록하세요")
        errors = [verify_ed25519(key, x_timestamp, request.method, path, body, x_signature, skew) for key in keys]
        error = None if None in errors else errors[0]
    else:
        error = verify(bank.hmac_secret, x_timestamp, request.method, path, body, x_signature, skew)
    if error:
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
