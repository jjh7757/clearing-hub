"""미정산 순채무(exposure) 계산 (SETTLEMENT_PLAN 4.4).

`reserve` 도메인은 `transfers`를 읽지 않으므로(2절 1번) 계산은 여기서 하고, 값만 넘긴다.
이 모듈은 `transfers.models`만 import한다 (`reserve`를 import하면 순환이 생긴다).
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.domains.transfers.models import Transfer, TransferStatus

# 보낸 쪽: 실패로 끝난 것만 빼고 전부 "나갈 수 있는 돈"이다. 진행 중(PENDING·UNKNOWN)도 넣는다 (4.4)
SENT_OPEN = (TransferStatus.ACCEPTED, TransferStatus.DELIVERING, TransferStatus.PENDING, TransferStatus.UNKNOWN,
             TransferStatus.COMPLETED)


async def open_exposure(db: AsyncSession, bank_code: str) -> int:
    """은행의 미정산 순채무 = sent_open − recv_open. 음수면 받은 돈이 더 많다는 뜻이고, 그만큼 더 보낼 수 있다.

    - sent_open: 그 은행이 보낸 거래 중 FAILED가 아닌 것의 합
    - recv_open: 그 은행이 받은 거래 중 COMPLETED인 것의 합 (아직 안 들어온 돈으로 보내게 하지 않는다)
    """
    sent_open = await db.scalar(
        select(func.coalesce(func.sum(Transfer.amount), 0))
        .where(Transfer.from_bank == bank_code, Transfer.status.in_(SENT_OPEN))
        # TODO 3단계: AND settlement_run_id IS NULL (정산된 거래는 예치금에 이미 반영됐으므로 뺀다)
    )
    recv_open = await db.scalar(
        select(func.coalesce(func.sum(Transfer.amount), 0))
        .where(Transfer.to_bank == bank_code, Transfer.status == TransferStatus.COMPLETED)
        # TODO 3단계: AND settlement_run_id IS NULL
    )
    return int(sent_open) - int(recv_open)
