from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field


def as_utc(value: datetime | None) -> datetime | None:
    """시간대가 빠진 시각(SQLite가 돌려주는 값)은 UTC로 본다. 허브는 시각을 UTC로 저장한다."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]


class SettlementRunCreate(BaseModel):
    """POST /admin/settlements/run 본문. 본문을 생략해도 된다 (SETTLEMENT_PLAN 6.2)."""

    settle_date: date | None = Field(default=None, description="정산일 YYYY-MM-DD. 생략하면 어제, 오늘이면 지금 즉시 마감")


class SettlementRunOut(BaseModel):
    run_id: int
    settle_date: date
    cutoff_at: UtcDatetime = Field(description="이 시각 전에 COMPLETED 된 미정산 거래까지 모았다")
    status: str
    tx_count: int
    total_amount: int
    created_by: str
    created_at: UtcDatetime
    settled_at: UtcDatetime | None
    settled_by: str | None


class PositionOut(BaseModel):
    bank_code: str
    sent_count: int
    sent_total: int = Field(description="보낸 금액 합 (줄 돈)")
    recv_count: int
    recv_total: int = Field(description="받은 금액 합 (받을 돈)")
    net_amount: int = Field(description="recv_total − sent_total. 양수면 받고 음수면 낸다")
    result: str | None = Field(description="예치금 반영 결과 (확정 뒤): OK / OVERDRAWN / OVERDUE")
    balance_after: int | None = Field(description="반영 뒤 예치금 잔액 (확정 뒤)")


class PairOut(BaseModel):
    bank_a: str
    bank_b: str
    a_to_b_count: int
    a_to_b_total: int
    b_to_a_count: int
    b_to_a_total: int
    net_amount: int = Field(description="두 방향 합계 차이의 절댓값")
    net_direction: str = Field(description="a_to_b / b_to_a / none")


class SettlementDetail(SettlementRunOut):
    positions: list[PositionOut] = Field(description="은행별 순 포지션, 은행 코드순. 합은 0")
    pairs: list[PairOut] = Field(description="은행 쌍별 표 (화면·CSV용)")


class SettlementTransferOut(BaseModel):
    transfer_id: str
    from_bank: str
    to_bank: str
    amount: int
    finished_at: UtcDatetime
    holder_name: str | None
