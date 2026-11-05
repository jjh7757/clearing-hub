"""허브 → 은행 호출 (SPEC 4절). 서명을 붙이고, 타임아웃·오류를 하나의 결과 형태로 돌려준다."""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from clearing_hub.core.config import get_settings
from clearing_hub.core.signing import HUB_BANK_CODE, sign
from clearing_hub.domains.banks.models import Bank

logger = logging.getLogger(__name__)


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
    def error(self) -> str:
        return str(self.body.get("error") or ("RECEIVER_TIMEOUT" if self.status is None else "RECEIVER_ERROR"))


class BankClient:
    """테스트에서는 `client`에 ASGITransport를 쓰는 httpx 클라이언트를 넣는다."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _request(self, bank: Bank, method: str, path: str, payload: dict | None = None) -> BankReply:
        url = bank.base_url.rstrip("/") + path
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else b""
        headers = {"X-Bank-Code": HUB_BANK_CODE, "Content-Type": "application/json; charset=utf-8", **sign(bank.hmac_secret, body)}
        timeout = get_settings().receiver_timeout_seconds
        try:
            # ASGITransport에는 httpx 타임아웃이 걸리지 않으므로 wait_for로 감싼다
            response = await asyncio.wait_for(
                self.client.request(method, url, content=body, headers=headers, timeout=timeout), timeout
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.warning("은행 %s 응답 없음: %s %s (%s)", bank.bank_code, method, path, type(exc).__name__)
            return BankReply(None, {})
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        return BankReply(response.status_code, data if isinstance(data, dict) else {})

    async def inquiry(self, bank: Bank, account_num: str) -> BankReply:
        return await self._request(bank, "POST", "/inquiry", {"account_num": account_num})

    async def receive(self, bank: Bank, payload: dict) -> BankReply:
        return await self._request(bank, "POST", "/receive", payload)

    async def receive_status(self, bank: Bank, transfer_id: str) -> BankReply:
        return await self._request(bank, "GET", f"/receive/{transfer_id}")

    async def accounts(self, bank: Bank, ci: str) -> BankReply:
        return await self._request(bank, "POST", "/accounts", {"ci": ci})


bank_client = BankClient()
