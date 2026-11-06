from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ReserveSummary(BaseModel):
    """예치금 요약 (SETTLEMENT_PLAN 6.1). available = balance + net_debit_cap − exposure"""

    bank_code: str
    balance: int = Field(description="예치금 잔액(원). 마이너스 가능")
    net_debit_cap: int = Field(description="순채무한도(원)")
    exposure: int = Field(description="미정산 순채무(원) (SETTLEMENT_PLAN 4.4)")
    available: int = Field(description="더 보낼 수 있는 금액(원)")


class ReserveAdmin(ReserveSummary):
    updated_at: datetime | None = Field(description="마지막 변동 시각. 계좌가 아직 없으면 null")


class ReserveMovement(BaseModel):
    """예치·인출 (DEPOSIT / WITHDRAW)."""

    amount: int = Field(ge=1, description="금액(원), 1 이상")
    memo: str = Field(default="", max_length=200)


class ReserveAdjust(BaseModel):
    """정정 분개 (ADJUST). 양수는 늘리고 음수는 줄인다. 한도 검사를 하지 않으므로 사유를 꼭 남긴다."""

    amount: int = Field(description="잔액 증감(원). 0은 안 된다")
    memo: str = Field(min_length=1, max_length=200, description="정정 사유")

    @field_validator("amount")
    @classmethod
    def not_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("amount는 0일 수 없습니다")
        return value


class LedgerEntryOut(BaseModel):
    entry_id: int
    bank_code: str
    kind: str
    amount: int
    run_id: int | None
    balance_after: int
    memo: str
    created_by: str
    created_at: datetime


class ReservePosted(BaseModel):
    """입출금·정정 응답: 남긴 분개와 바뀐 잔액."""

    entry: LedgerEntryOut
    reserve: ReserveSummary
