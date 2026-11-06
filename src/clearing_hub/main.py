import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import engine
from clearing_hub.core.exception_handlers import register_exception_handlers
from clearing_hub.core.log_setup import configure_logging
from clearing_hub.core.request_id import RequestIdMiddleware
from clearing_hub.core.response_signing import ResponseSigningMiddleware
from clearing_hub.domains.accounts.router import admin_router as account_admin_router
from clearing_hub.domains.accounts.router import router as account_router
from clearing_hub.domains.banks.router import admin_router as bank_admin_router
from clearing_hub.domains.banks.router import invite_admin_router
from clearing_hub.domains.banks.router import keys_router
from clearing_hub.domains.banks.router import router as bank_router
from clearing_hub.domains.inquiry.router import router as inquiry_router
from clearing_hub.domains.reserve.router import admin_router as reserve_admin_router
from clearing_hub.domains.reserve.router import router as reserve_router
from clearing_hub.domains.settlement.router import admin_router as settlement_admin_router
from clearing_hub.domains.settlement.router import router as settlement_router
from clearing_hub.domains.transfers.router import admin_router as transfer_admin_router
from clearing_hub.domains.transfers.router import router as transfer_router
from clearing_hub.workers.retry import retry_loop
from clearing_hub.workers.settlement import settlement_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(retry_loop())]  # 미확인 거래 재시도
    if get_settings().settlement_worker_enabled:
        tasks.append(asyncio.create_task(settlement_loop()))  # 일일 정산 배치 (SETTLEMENT_PLAN 5.1)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()


configure_logging()  # JSON 로그, 모든 줄에 request_id (SPEC 2.4)
app = FastAPI(title="ClearingHub", lifespan=lifespan)


def _hub_signing_key() -> str:
    return get_settings().hub_signing_key.get_secret_value()  # 필수 설정 (core/config.py)


# 나중에 더한 미들웨어가 바깥이다: RequestId가 가장 바깥이어야 처리하지 못한 500에도 X-Request-ID가 붙는다
app.add_middleware(ResponseSigningMiddleware, key_getter=_hub_signing_key, path_prefix="/v1/")  # 응답 서명 (SPEC 2.5)
app.add_middleware(RequestIdMiddleware)  # X-Request-ID 받기·만들기·응답에 붙이기 (SPEC 2.4)
register_exception_handlers(app)
app.include_router(bank_router)
app.include_router(keys_router)
app.include_router(inquiry_router)
app.include_router(transfer_router)
app.include_router(account_router)
app.include_router(reserve_router)
app.include_router(settlement_router)
app.include_router(bank_admin_router)
app.include_router(invite_admin_router)
app.include_router(transfer_admin_router)
app.include_router(account_admin_router)
app.include_router(reserve_admin_router)
app.include_router(settlement_admin_router)


@app.get("/")
async def root():
    return {"service": "ClearingHub", "spec": "v1.0"}  # docs/SPEC.md 버전과 맞춘다
