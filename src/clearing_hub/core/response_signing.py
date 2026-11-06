"""응답 서명 미들웨어 (SPEC 2.5, SETTLEMENT_PLAN 12.2 3번).

message = X-Response-Timestamp + "\\n" + STATUS + "\\n" + 요청의 X-Signature 값 + "\\n" + 응답 본문 바이트
헤더   = X-Response-Timestamp, X-Response-Signature, X-Signature-Alg: ed25519

- 허브: `/v1/*`의 2xx·4xx 응답에 허브 키(HUB_SIGNING_KEY, 필수)로 항상 붙인다. 은행은 3.3 응답 등을 허브 공개키로 검증한다.
- 가짜 은행(bank_sim): 같은 클래스로 `/interbank/*` 응답에 자기 키(SIM_SIGNING_KEY)로 붙인다.
- 5xx는 붙이지 않는다 (본문 보장이 없다, SPEC 5절). 본문은 한 바이트도 바꾸지 않는다.
- 본문을 다 모아야 서명할 수 있으므로, 여러 조각으로 나오는 스트리밍 응답은 서명 없이 그대로 흘려보낸다.
  이 프로젝트의 API 응답은 모두 한 조각(JSONResponse)이다.
- 순수 ASGI 미들웨어라 요청 본문은 건드리지 않는다 (요청 서명 검증은 본문 바이트 그대로 한다).
"""

from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from clearing_hub.core.signing import sign_response


class ResponseSigningMiddleware:
    def __init__(self, app: ASGIApp, key_getter: Callable[[], str | None], path_prefix: str) -> None:
        self.app = app
        self.key_getter = key_getter  # 개인키 base64. 요청마다 읽는다 (없으면 서명하지 않음: 테스트에서 "서명 없는 응답"을 만들 때)
        self.path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.path_prefix):
            await self.app(scope, receive, send)
            return
        key = self.key_getter()
        if not key:
            await self.app(scope, receive, send)
            return
        request_signature = next(
            (value.decode("latin-1") for name, value in scope.get("headers", []) if name == b"x-signature"), ""
        )
        start: Message | None = None
        passthrough = False

        async def send_signed(message: Message) -> None:
            nonlocal start, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                if message["status"] >= 500:
                    passthrough = True
                    await send(message)
                else:
                    start = message  # 본문을 보고 서명한 뒤 보낸다
                return
            if message["type"] == "http.response.body" and start is not None:
                if message.get("more_body", False):  # 스트리밍: 서명 없이 그대로
                    passthrough = True
                    await send(start)
                    await send(message)
                    return
                body = message.get("body", b"")
                signed = sign_response(key, start["status"], request_signature, body)
                names = {name.lower().encode("latin-1") for name in signed}
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() not in names]
                headers += [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in signed.items()]
                await send({**start, "headers": headers})
                start = None
                passthrough = True
                await send(message)
                return
            await send(message)

        await self.app(scope, receive, send_signed)
