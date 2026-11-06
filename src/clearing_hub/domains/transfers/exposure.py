"""미정산 순채무(exposure) 계산 (SETTLEMENT_PLAN 4.4).

`reserve` 도메인은 `transfers`를 읽지 않으므로(2절 1번) 계산은 여기서 하고, 값만 넘긴다.
이 모듈은 `transfers.models`와 `settlement.models`만 import한다 (`reserve`를 import하면 순환이 생긴다.
`settlement.models`는 아무 도메인도 import하지 않는다).
"""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from clearing_hub.domains.settlement.models import SettlementRun, SettlementStatus
from clearing_hub.domains.transfers.models import Transfer, TransferStatus

# 보낸 쪽: 실패로 끝난 것만 빼고 전부 "나갈 수 있는 돈"이다. 진행 중(PENDING·UNKNOWN)도 넣는다 (4.4)
SENT_OPEN = (TransferStatus.ACCEPTED, TransferStatus.DELIVERING, TransferStatus.PENDING, TransferStatus.UNKNOWN,
             TransferStatus.COMPLETED)

# 예치금에 아직 반영되지 않은 거래: 정산에 안 들어갔거나, 들어간 run이 아직 SETTLED가 아니다(DRAFT).
# DRAFT 거래를 빼면 확정 전까지 예치금도 순채무도 그 금액을 세지 않아, 그 사이에 한도만큼 한 번 더 보낼 수 있게 된다.
# 확정(4단계)은 run을 SETTLED로 바꾸고 예치금을 같은 트랜잭션에서 옮기므로, 순채무와 잔액이 한꺼번에 바뀐다
UNSETTLED = or_(Transfer.settlement_run_id.is_(None),
                Transfer.settlement_run_id.in_(select(SettlementRun.run_id)
                                               .where(SettlementRun.status != SettlementStatus.SETTLED)))


async def open_exposure(db: AsyncSession, bank_code: str) -> int:
    """은행의 미정산 순채무 = sent_open − recv_open. 음수면 받은 돈이 더 많다는 뜻이고, 그만큼 더 보낼 수 있다.

    - sent_open: 그 은행이 보낸 거래 중 FAILED가 아닌 것의 합
    - recv_open: 그 은행이 받은 거래 중 COMPLETED인 것의 합 (아직 안 들어온 돈으로 보내게 하지 않는다)
    둘 다 예치금에 아직 반영되지 않은 거래(UNSETTLED)만 센다. 확정된 정산의 거래는 예치금 잔액에 이미 들어 있다.
    """
    sent_open = await db.scalar(
        select(func.coalesce(func.sum(Transfer.amount), 0))
        .where(Transfer.from_bank == bank_code, Transfer.status.in_(SENT_OPEN), UNSETTLED)
    )
    recv_open = await db.scalar(
        select(func.coalesce(func.sum(Transfer.amount), 0))
        .where(Transfer.to_bank == bank_code, Transfer.status == TransferStatus.COMPLETED, UNSETTLED)
    )
    return int(sent_open) - int(recv_open)
