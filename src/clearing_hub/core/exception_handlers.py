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


async def api_exception_handler(request: Request, exc: ApiException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message, exc.transfer_id))


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
        exc.status_code, "HTTP_ERROR"
    )
    message = exc.detail if isinstance(exc.detail, str) else "요청을 처리할 수 없습니다"
    return JSONResponse(status_code=exc.status_code, content=error_body(code, message), headers=exc.headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = ", ".join(".".join(str(p) for p in e["loc"] if p != "body") for e in exc.errors())
    return JSONResponse(status_code=422, content=error_body("VALIDATION_ERROR", f"입력값을 확인해 주세요: {fields}"))


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("처리하지 못한 오류: %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content=error_body("INTERNAL_ERROR", "서버에서 오류가 발생했습니다"))


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiException, api_exception_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
