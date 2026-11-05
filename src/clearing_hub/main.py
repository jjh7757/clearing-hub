import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from clearing_hub.core.database import engine
from clearing_hub.core.exception_handlers import register_exception_handlers
from clearing_hub.domains.banks.router import admin_router as bank_admin_router
from clearing_hub.domains.banks.router import router as bank_router
from clearing_hub.domains.inquiry.router import router as inquiry_router
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


app = FastAPI(title="ClearingHub", lifespan=lifespan)
register_exception_handlers(app)
app.include_router(bank_router)
app.include_router(inquiry_router)
app.include_router(transfer_router)
app.include_router(bank_admin_router)
app.include_router(transfer_admin_router)


@app.get("/")
async def root():
    return {"service": "ClearingHub", "spec": "v0.1"}
