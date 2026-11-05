"""요청 추적 ID (SPEC 2.4 `X-Request-ID`, 선택).

- 보내는 은행이 보낸 `X-Request-ID`가 형식에 맞으면 그대로 쓰고, 없거나 틀리면 허브가 새로 만든다 (거절하지 않는다).
- 응답에도 같은 값을 붙이고, 그 요청으로 받는 은행을 부를 때 그대로 전달한다.
- 이체는 거래에 저장해 두고 재시도(4.2 재전송, 4.3 확인)에도 같은 값을 보낸다. 세 서버 로그를 한 ID로 이어 볼 수 있다.
- 서명 대상이 아니다. 로그 추적용이라 바뀌어도 돈의 흐름에는 영향이 없다.
"""

import re
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


def valid_request_id(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if REQUEST_ID_PATTERN.fullmatch(value) else None


class RequestIdMiddleware:
    """순수 ASGI 미들웨어: 요청 본문을 건드리지 않는다 (서명 검증은 본문 바이트 그대로 한다)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next(
            (value.decode("latin-1") for name, value in scope.get("headers", []) if name == b"x-request-id"), None
        )
        request_id = valid_request_id(incoming) or uuid.uuid4().hex
        token = _request_id.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)
