"""미확인 거래 재시도 태스크. 앱 lifespan에서 시작해 retry_interval_seconds마다 confirm_pending을 돌린다."""

import asyncio
import logging

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import SessionLocal
from clearing_hub.domains.accounts.service import purge_expired
from clearing_hub.domains.transfers.service import confirm_pending

logger = logging.getLogger(__name__)


async def retry_loop() -> None:
    interval = get_settings().retry_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            async with SessionLocal() as db:
                confirmed = await confirm_pending(db)
            if confirmed:
                logger.info("미확인 거래 %d건 확정", confirmed)
            async with SessionLocal() as db:
                await purge_expired(db)  # 만료된 account_ref, 24시간 지난 ci_hash (SPEC 9.5)
        except Exception:  # 태스크가 죽으면 재시도가 영영 멈추므로 기록만 하고 계속 돈다
            logger.exception("재시도 태스크 오류")
