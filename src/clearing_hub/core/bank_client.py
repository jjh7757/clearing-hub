"""허브 → 은행 호출 (SPEC 4절). 서명을 붙이고, 타임아웃·오류를 하나의 결과 형태로 돌려준다.

허브는 모든 은행에 허브 키(HUB_SIGNING_KEY)로 Ed25519 서명한다 (SPEC 2절). 은행의 2xx·4xx 응답에는 응답 서명(SPEC 2.5)이
**반드시** 있어야 하고, 없거나 틀리면 그 응답을 "응답 없음"으로 바꾼다. 2xx를 입금 완료로 믿지 않고 4.3 확인으로 넘긴다 (12.4).
FAILED로 끝내면 받는 은행이 실제로 입금했을 때 보내는 은행이 되돌려 이중 지급이 되기 때문이다. 5xx는 서명을 요구하지 않는다
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from clearing_hub.core.config import get_settings
from clearing_hub.core.request_id import REQUEST_ID_HEADER, get_request_id
from clearing_hub.core.signing import (
    ALG_ED25519, ALG_HEADER, HUB_BANK_CODE, RESPONSE_SIGNATURE_HEADER, RESPONSE_TIMESTAMP_HEADER, sign_ed25519,
    verify_response,
)
from clearing_hub.domains.banks.models import Bank
from clearing_hub.domains.support import failures

logger = logging.getLogger(__name__)

# 받는 은행이 거절할 때 쓸 수 있는 코드 (SPEC 4.1·4.2 규칙 4·4.3, 5절 표)
RECEIVER_REJECT_CODES = {"ACCOUNT_NOT_FOUND", "ACCOUNT_INACTIVE", "AMOUNT_INVALID", "LIMIT_EXCEEDED", "VALIDATION_ERROR", "TRANSFER_NOT_FOUND"}

# 증거로 남길 헤더 (SETTLEMENT_PLAN 12.3). 서명 검증에 필요한 것과 추적 ID만
REQUEST_EVIDENCE_HEADERS = ("X-Bank-Code", "X-Timestamp", "X-Signature", ALG_HEADER, REQUEST_ID_HEADER)
REPLY_EVIDENCE_HEADERS = (RESPONSE_TIMESTAMP_HEADER, RESPONSE_SIGNATURE_HEADER, ALG_HEADER)


@dataclass
class Exchange:
    """실제로 오간 바이트 (증거 보관용, transfers/evidence.py). 응답을 못 받았으면 response_status가 None."""

    method: str
    path: str  # 서명에 쓴 규격 경로 (base_url 뒤의 부분)
    request_headers: dict[str, str]
    request_body: bytes
    signature_alg: str
    response_status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: bytes = b""
    url: str | None = None  # 실제로 부른 주소 (base_url + path). 인증 실패 기록용 (증거에는 남기지 않는다)


@dataclass
class BankReply:
    status: int | None  # None = 응답 없음(타임아웃·연결 실패, 2xx·4xx인데 응답 서명이 없거나 틀림)
    body: dict[str, Any]
    # 응답 서명 검증 결과: True 맞음, False 없음·틀림, None 응답을 못 받음 또는 5xx(서명을 요구하지 않음)
    signature_valid: bool | None = None
    exchange: Exchange | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def rejected(self) -> bool:
        """은행이 4xx로 거절. 재시도하지 않는다."""
        return self.status is not None and 400 <= self.status < 500

    @property
    def no_answer(self) -> bool:
        """타임아웃·연결 실패·5xx. 결과를 모르므로 재시도 대상."""
        return not self.ok and not self.rejected

    @property
    def auth_failed(self) -> bool:
        """받는 은행이 허브의 서명·인증을 거절(401·403). 보내는 은행 잘못이 아니라 허브 ↔ 받는 은행 설정 문제다.
        은행은 서명 검증을 처리보다 먼저 하므로(SPEC 4절) 입금은 되지 않았다."""
        return self.status in (401, 403)

    @property
    def error(self) -> str:
        """보내는 은행에 알릴 오류 코드. 받는 은행의 인증 오류(SIGNATURE_INVALID 등)는 보내는 은행이
        자기 서명이 틀렸다고 오해하지 않게 RECEIVER_ERROR로 바꾼다 (SPEC 5절)."""
        if self.auth_failed:
            return "RECEIVER_ERROR"
        code = self.body.get("error")
        if self.rejected:
            # 받는 은행이 SPEC 4.2 규칙 4·5절 표에 없는 코드로 거절하면 표의 RECEIVER_ERROR로 바꾼다 (설명은 그대로 넘긴다)
            return code if code in RECEIVER_REJECT_CODES else "RECEIVER_ERROR"
        return str(code) if isinstance(code, str) and code else ("RECEIVER_TIMEOUT" if self.status is None else "RECEIVER_ERROR")

    @property
    def message(self) -> str | None:
        if self.auth_failed:
            return "받는 은행과 허브의 연결 설정에 문제가 있습니다. 허브 관리자에게 알려 주세요"
        message = self.body.get("message")
        return str(message) if message else None


def observed(reply: BankReply) -> tuple[int | None, dict[str, Any]]:
    """은행이 실제로 돌려준 HTTP 상태와 본문. 응답 서명이 틀려 BankReply가 "응답 없음"이 됐어도 오간 원문(exchange)에서
    꺼낸다 (ping·셀프 시험 결과를 은행에게 보여 줘 고칠 곳을 찾게). 응답을 못 받았으면 (None, {})."""
    ex = reply.exchange
    status = ex.response_status if ex else reply.status
    try:
        body = json.loads(ex.response_body) if ex and ex.response_body else reply.body
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _pick(headers, names: tuple[str, ...]) -> dict[str, str]:
    return {name: headers[name] for name in names if headers.get(name) is not None}


def _sign(method: str, path: str, body: bytes) -> dict[str, str]:
    """허브 키로 Ed25519 서명 (SPEC 2.2). X-Signature-Alg: ed25519를 붙인다."""
    return sign_ed25519(get_settings().hub_signing_key.get_secret_value(), method, path, body)


def _check_reply_signature(bank: Bank, response: httpx.Response, request_signature: str) -> bool:
    """은행 응답 서명(SPEC 2.5) 검증. 은행이 키를 교체한 직후일 수 있어 이전 키(24시간 안)로도 확인한다.
    공개키가 없는 은행(8c 전 등록)은 검증할 키가 없어 항상 False다."""
    timestamp = response.headers.get(RESPONSE_TIMESTAMP_HEADER)
    signature = response.headers.get(RESPONSE_SIGNATURE_HEADER)
    if not signature:
        return False
    if (response.headers.get(ALG_HEADER) or ALG_ED25519).strip().lower() != ALG_ED25519:
        return False
    skew = get_settings().signature_max_skew_seconds
    return any(
        verify_response(key, timestamp, response.status_code, request_signature, response.content, signature, skew) is None
        for key in bank.verification_keys()
    )


class BankClient:
    """테스트에서는 `client`에 ASGITransport를 쓰는 httpx 클라이언트를 넣는다."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _request(
        self, bank: Bank, method: str, path: str, payload: dict | None = None, request_id: str | None = None,
        base_url: str | None = None,
    ) -> BankReply:
        url = (base_url or bank.base_url).rstrip("/") + path
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else b""
        # 서명 경로는 base_url 뒤의 부분이다 (SPEC 2.2). 은행 앞단 프록시가 경로를 바꿔도 맞게 하기 위해서다
        headers = {"X-Bank-Code": HUB_BANK_CODE, "Content-Type": "application/json; charset=utf-8",
                   **_sign(method, path, body)}
        # 추적 ID (SPEC 2.4): 거래에 저장된 값 > 지금 요청의 값. 재시도 태스크처럼 요청 밖이면 저장된 값만 쓴다
        request_id = request_id or get_request_id()
        if request_id:
            headers[REQUEST_ID_HEADER] = request_id
        exchange = Exchange(method, path, _pick(headers, REQUEST_EVIDENCE_HEADERS), body, headers[ALG_HEADER], url=url)
        timeout = get_settings().receiver_timeout_seconds
        try:
            # ASGITransport에는 httpx 타임아웃이 걸리지 않으므로 wait_for로 감싼다
            response = await asyncio.wait_for(
                self.client.request(method, url, content=body, headers=headers, timeout=timeout), timeout
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.warning("은행 %s 응답 없음: %s %s (%s)", bank.bank_code, method, path, type(exc).__name__,
                           extra={"request_id": request_id, "bank_code": bank.bank_code})
            return BankReply(None, {}, exchange=exchange)
        exchange.response_status = response.status_code
        exchange.response_headers = _pick(response.headers, REPLY_EVIDENCE_HEADERS)
        exchange.response_body = response.content
        signature_valid = None if response.status_code >= 500 else _check_reply_signature(bank, response, headers["X-Signature"])
        # 은행이 401·403을 줬거나 응답 서명이 틀리면 연동 진단용으로 남긴다 (기다리지 않는다, domains/support/failures.py)
        failures.record_reply(bank, BankReply(None, {}, signature_valid, exchange))
        if signature_valid is False:
            # 서명 없는·틀린 응답은 그 은행이 답했다고 증명할 수 없다. 2xx라도 입금 완료로 믿지 않는다 (12.4).
            # 5xx는 본문 보장이 없어(SPEC 5절) 서명을 요구하지 않고 그대로 5xx(재시도)로 본다
            logger.error("은행 %s 응답 서명 검증 실패: %s %s HTTP %s → 응답 없음으로 처리", bank.bank_code, method, path,
                         response.status_code, extra={"request_id": request_id, "bank_code": bank.bank_code})
            return BankReply(None, {}, signature_valid=False, exchange=exchange)
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        return BankReply(response.status_code, data if isinstance(data, dict) else {}, signature_valid, exchange)

    async def inquiry(self, bank: Bank, account_num: str, request_id: str | None = None,
                      base_url: str | None = None) -> BankReply:
        """base_url을 주면 등록된 주소 대신 그 주소로 부른다 (PATCH /v1/me가 새 주소를 반영하기 전에 ping할 때)."""
        return await self._request(bank, "POST", "/inquiry", {"account_num": account_num}, request_id, base_url)

    async def receive(self, bank: Bank, payload: dict, request_id: str | None = None) -> BankReply:
        return await self._request(bank, "POST", "/receive", payload, request_id)

    async def receive_status(self, bank: Bank, transfer_id: str, request_id: str | None = None) -> BankReply:
        return await self._request(bank, "GET", f"/receive/{transfer_id}", request_id=request_id)

    async def accounts(self, bank: Bank, ci: str) -> BankReply:
        return await self._request(bank, "POST", "/accounts", {"ci": ci})


bank_client = BankClient()
