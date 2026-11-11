"""SimBank(902) → 허브 호출 (SPEC 3절): 서명해서 부르고, 허브 응답 서명을 검증한다 (2.5).

환경 변수 (요청마다 읽는다. 테스트가 monkeypatch로 바꾼다)
  SIM_HUB_URL         허브 주소 (운영 http://clearing-hub:8000). `/v1`은 붙이지 않는다
  SIM_API_KEY         허브가 902에 준 API 키 (X-Api-Key, SPEC 2.1)
  SIM_SIGNING_KEY     이 은행의 Ed25519 개인키 (요청 서명. /interbank 응답 서명과 같은 키)
  SIM_HUB_PUBLIC_KEY  허브 공개키. 있으면 허브 응답 서명을 검증하고, 비어 있으면 건너뛴다 (로컬용, verify_hub와 같은 원칙)
  SIM_BANK_CODE       이 은행 코드 (기본 902)

테스트는 `hub_client.client`에 허브 ASGI 앱을 붙인 httpx.AsyncClient를 넣는다 (그러면 SIM_HUB_URL은 보지 않는다).
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from clearing_hub.core.signing import sign_ed25519, verify_response

logger = logging.getLogger("bank_sim.hub")

TIMEOUT_SECONDS = 9.0  # 허브는 받는 은행을 5초까지 기다린다 (RECEIVER_TIMEOUT). 그보다 길고 10초 안쪽


def bank_code() -> str:
    return os.environ.get("SIM_BANK_CODE") or "902"


@dataclass
class HubReply:
    """허브 응답. `status`가 None이면 응답을 못 받았다(연결 오류·타임아웃).
    `verified`가 False면 응답 서명이 없거나 틀렸다 → 본문을 믿지 않는다 (SPEC 2.5)."""
    status: int | None
    body: dict[str, Any] = field(default_factory=dict)
    verified: bool = True

    @property
    def trusted(self) -> bool:
        """응답을 받았고 서명도 맞는 2xx·4xx."""
        return self.status is not None and self.status < 500 and self.verified


class HubClient:
    def __init__(self) -> None:
        self.client: httpx.AsyncClient | None = None  # 테스트가 바꿔 끼운다

    def _client(self) -> httpx.AsyncClient | None:
        if self.client is None:
            url = os.environ.get("SIM_HUB_URL", "").rstrip("/")
            if not url:
                return None
            self.client = httpx.AsyncClient(base_url=url, timeout=TIMEOUT_SECONDS)
        return self.client

    def configured(self) -> bool:
        return self.client is not None or bool(os.environ.get("SIM_HUB_URL"))

    async def call(self, method: str, path: str, payload: dict | None = None) -> HubReply:
        """`path`는 `/v1/...` (서명 PATH와 같다, SPEC 2.2)."""
        client = self._client()
        if client is None:
            return HubReply(None, {"error": "HUB_NOT_CONFIGURED", "message": "SIM_HUB_URL이 없습니다"})
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else b""
        signed = sign_ed25519(os.environ.get("SIM_SIGNING_KEY", ""), method, path, body)
        headers = {"X-Bank-Code": bank_code(), "X-Api-Key": os.environ.get("SIM_API_KEY", ""),
                   "Content-Type": "application/json", **signed}
        try:
            res = await client.request(method, path, content=body, headers=headers, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:  # 연결 오류·타임아웃: 결과를 모른다
            logger.warning("허브 호출 실패 %s %s: %s", method, path, type(exc).__name__)
            return HubReply(None, {"error": "HUB_UNREACHABLE", "message": "허브에 연결하지 못했습니다"})
        try:
            data = res.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        verified = True
        hub_key = os.environ.get("SIM_HUB_PUBLIC_KEY")
        if hub_key and res.status_code < 500:  # 5xx는 서명이 없어도 된다 (SPEC 2.5). 어차피 결과를 모르는 것으로 본다
            error = verify_response(hub_key, res.headers.get("x-response-timestamp"), res.status_code,
                                    signed["X-Signature"], res.content, res.headers.get("x-response-signature"))
            if error:
                verified = False
                logger.warning("허브 응답 서명 검증 실패 %s %s: %s", method, path, error)
        return HubReply(res.status_code, data, verified)


hub = HubClient()


def transfer_outcome(reply: HubReply) -> tuple[str | None, str | None, str | None]:
    """3.3 응답 또는 3.4 응답 → (확정 상태, fail_code, fail_message). 확정 상태가 None이면 아직 모른다 → PENDING 유지.

    SPEC 3.3: 되돌릴지는 본문의 `status`로만 정한다. `FAILED`만 되돌리고 202·`status` 없는 4xx·5xx·타임아웃·
    서명 검증 실패는 되돌리지 않는다 (돈이 이미 나갔을 수 있다)."""
    if not reply.trusted:
        return None, None, None
    status = reply.body.get("status")
    if status == "COMPLETED" and reply.status == 200:
        return "COMPLETED", None, None
    if status == "FAILED":
        code = reply.body.get("error") or reply.body.get("fail_code") or "FAILED"
        message = reply.body.get("message") or reply.body.get("fail_message") or ""
        return "FAILED", code, message
    return None, None, None
