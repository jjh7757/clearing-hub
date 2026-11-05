"""요청 인증 의존성: 참가 은행(서명), 관리자(토큰)."""

import hmac
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import get_db
from clearing_hub.core.exceptions import Unauthorized
from clearing_hub.core.signing import hash_api_key, verify
from clearing_hub.domains.banks import service as bank_service
from clearing_hub.domains.banks.models import Bank


async def current_bank(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_bank_code: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_timestamp: Annotated[str | None, Header()] = None,
    x_signature: Annotated[str | None, Header()] = None,
) -> Bank:
    """X-Bank-Code·X-Api-Key로 은행을 찾고, 그 은행의 secret으로 서명을 검증한다 (SPEC 2)."""
    if not (x_bank_code and x_api_key and x_timestamp and x_signature):
        raise Unauthorized(message="인증 헤더가 없습니다")
    bank = await bank_service.get_bank(db, x_bank_code)
    if bank is None or not hmac.compare_digest(bank.api_key_hash, hash_api_key(x_api_key)):
        raise Unauthorized(message="은행 코드 또는 API 키가 맞지 않습니다")
    if not bank.active:
        raise Unauthorized(code="BANK_INACTIVE", message="비활성 은행입니다")
    body = await request.body()
    error = verify(bank.hmac_secret, x_timestamp, body, x_signature, get_settings().signature_max_skew_seconds)
    if error:
        raise Unauthorized(code=error, message="서명이 맞지 않습니다" if error == "SIGNATURE_INVALID" else "서명 시각이 만료됐습니다")
    return bank


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().admin_token.get_secret_value()
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise Unauthorized(message="관리자 토큰이 맞지 않습니다")
