import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from clearing_hub.core.database import engine
from clearing_hub.core.exception_handlers import register_exception_handlers
from clearing_hub.core.log_setup import configure_logging
from clearing_hub.core.request_id import RequestIdMiddleware
from clearing_hub.domains.accounts.router import admin_router as account_admin_router
from clearing_hub.domains.accounts.router import router as account_router
from clearing_hub.domains.banks.router import admin_router as bank_admin_router
from clearing_hub.domains.banks.router import router as bank_router
from clearing_hub.domains.inquiry.router import router as inquiry_router
from clearing_hub.domains.reserve.router import admin_router as reserve_admin_router
from clearing_hub.domains.reserve.router import router as reserve_router
from clearing_hub.domains.settlement.router import admin_router as settlement_admin_router
from clearing_hub.domains.transfers.router import admin_router as transfer_admin_router
from clearing_hub.domains.transfers.router import router as transfer_router
from clearing_hub.workers.retry import retry_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(retry_loop())  # 미확인 거래 재시도
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await engine.dispose()


configure_logging()  # JSON 로그, 모든 줄에 request_id (SPEC 2.4)
app = FastAPI(title="ClearingHub", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)  # X-Request-ID 받기·만들기·응답에 붙이기 (SPEC 2.4)
register_exception_handlers(app)
app.include_router(bank_router)
app.include_router(inquiry_router)
app.include_router(transfer_router)
app.include_router(account_router)
app.include_router(reserve_router)
app.include_router(bank_admin_router)
app.include_router(transfer_admin_router)
app.include_router(account_admin_router)
app.include_router(reserve_admin_router)
app.include_router(settlement_admin_router)


@app.get("/")
async def root():
    return {"service": "ClearingHub", "spec": "v0.5.2"}  # docs/SPEC.md 버전과 맞춘다
