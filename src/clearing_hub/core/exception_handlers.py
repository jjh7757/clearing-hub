import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from clearing_hub.core.exceptions import ApiException

logger = logging.getLogger(__name__)


def error_body(code: str, message: str, transfer_id: str | None = None) -> dict:
    body = {"error": code, "message": message}
    if transfer_id:
        body["transfer_id"] = transfer_id
    return body


# SPEC 5절 표의 HTTP 상태. 받는 은행이 준 거절 코드를 보내는 은행에 넘길 때 쓴다 (나머지 거절은 400)
_SPEC_HTTP = {"ACCOUNT_NOT_FOUND": 404, "TRANSFER_NOT_FOUND": 404, "VALIDATION_ERROR": 422}


def spec_http_status(code: str) -> int:
    return _SPEC_HTTP.get(code, 400)


async def api_exception_handler(request: Request, exc: ApiException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message, exc.transfer_id))


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """프레임워크가 낸 HTTP 오류도 SPEC 5절 표의 코드로만 답한다.
    없는 경로·허용하지 않는 메서드는 404 NOT_FOUND ("없는 경로"), 그 밖의 4xx는 422 VALIDATION_ERROR, 5xx는 500 INTERNAL_ERROR."""
    if exc.status_code in (401, 403):
        status, code = exc.status_code, "UNAUTHORIZED" if exc.status_code == 401 else "FORBIDDEN"
        message = exc.detail if isinstance(exc.detail, str) else "인증에 실패했습니다"
    elif exc.status_code in (404, 405):
        status, code, message = 404, "NOT_FOUND", "없는 경로입니다. 주소와 메서드(GET·POST)를 확인해 주세요"
    elif exc.status_code < 500:
        status, code = 422, "VALIDATION_ERROR"
        message = f"요청을 처리할 수 없습니다: {exc.detail}" if isinstance(exc.detail, str) else "요청을 처리할 수 없습니다"
    else:
        status, code, message = 500, "INTERNAL_ERROR", "서버에서 오류가 발생했습니다"
    headers = {k: v for k, v in (exc.headers or {}).items() if k.lower() != "allow"} or None
    return JSONResponse(status_code=status, content=error_body(code, message), headers=headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 필드 오류는 필드 이름, 본문 전체 검사(model_validator) 오류는 그 설명을 보여 준다
    fields = ", ".join(
        ".".join(str(p) for p in e["loc"] if p != "body") or str(e.get("msg", "")).removeprefix("Value error, ")
        for e in exc.errors()
    )
    return JSONResponse(status_code=422, content=error_body("VALIDATION_ERROR", f"입력값을 확인해 주세요: {fields}"))


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("처리하지 못한 오류: %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content=error_body("INTERNAL_ERROR", "서버에서 오류가 발생했습니다"))


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiException, api_exception_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
