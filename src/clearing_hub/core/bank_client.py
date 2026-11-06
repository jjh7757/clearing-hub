"""허브 → 은행 호출 (SPEC 4절). 서명을 붙이고, 타임아웃·오류를 하나의 결과 형태로 돌려준다."""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from clearing_hub.core.config import get_settings
from clearing_hub.core.request_id import REQUEST_ID_HEADER, get_request_id
from clearing_hub.core.signing import HUB_BANK_CODE, sign
from clearing_hub.domains.banks.models import Bank

logger = logging.getLogger(__name__)

# 받는 은행이 거절할 때 쓸 수 있는 코드 (SPEC 4.1·4.2 규칙 4·4.3, 5절 표)
RECEIVER_REJECT_CODES = {"ACCOUNT_NOT_FOUND", "ACCOUNT_INACTIVE", "AMOUNT_INVALID", "LIMIT_EXCEEDED", "VALIDATION_ERROR", "TRANSFER_NOT_FOUND"}


@dataclass
class BankReply:
    status: int | None  # None = 응답 없음(타임아웃·연결 실패)
    body: dict[str, Any]

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
        self, bank: Bank, method: str, path: str, payload: dict | None = None, request_id: str | None = None
    ) -> BankReply:
        url = bank.base_url.rstrip("/") + path
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else b""
        # 서명 경로는 base_url 뒤의 부분이다 (SPEC 2.2). 은행 앞단 프록시가 경로를 바꿔도 맞게 하기 위해서다
        headers = {"X-Bank-Code": HUB_BANK_CODE, "Content-Type": "application/json; charset=utf-8",
                   **sign(bank.hmac_secret, method, path, body)}
        # 추적 ID (SPEC 2.4): 거래에 저장된 값 > 지금 요청의 값. 재시도 태스크처럼 요청 밖이면 저장된 값만 쓴다
        request_id = request_id or get_request_id()
        if request_id:
            headers[REQUEST_ID_HEADER] = request_id
        timeout = get_settings().receiver_timeout_seconds
        try:
            # ASGITransport에는 httpx 타임아웃이 걸리지 않으므로 wait_for로 감싼다
            response = await asyncio.wait_for(
                self.client.request(method, url, content=body, headers=headers, timeout=timeout), timeout
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.warning("은행 %s 응답 없음: %s %s (%s)", bank.bank_code, method, path, type(exc).__name__,
                           extra={"request_id": request_id, "bank_code": bank.bank_code})
            return BankReply(None, {})
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        return BankReply(response.status_code, data if isinstance(data, dict) else {})

    async def inquiry(self, bank: Bank, account_num: str, request_id: str | None = None) -> BankReply:
        return await self._request(bank, "POST", "/inquiry", {"account_num": account_num}, request_id)

    async def receive(self, bank: Bank, payload: dict, request_id: str | None = None) -> BankReply:
        return await self._request(bank, "POST", "/receive", payload, request_id)

    async def receive_status(self, bank: Bank, transfer_id: str, request_id: str | None = None) -> BankReply:
        return await self._request(bank, "GET", f"/receive/{transfer_id}", request_id=request_id)

    async def accounts(self, bank: Bank, ci: str) -> BankReply:
        return await self._request(bank, "POST", "/accounts", {"ci": ci})


bank_client = BankClient()
