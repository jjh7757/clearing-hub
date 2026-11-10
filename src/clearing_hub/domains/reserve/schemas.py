from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, field_validator

from clearing_hub.domains.transfers.schemas import spec_time  # SPEC 1절 시각 형식 (초 단위 UTC, Z)


class ReserveSummary(BaseModel):
    """예치금 요약 (SETTLEMENT_PLAN 6.1). available = balance + net_debit_cap − exposure"""

    bank_code: str
    balance: int = Field(description="예치금 잔액(원). 마이너스 가능")
    net_debit_cap: int = Field(description="순채무한도(원)")
    exposure: int = Field(description="미정산 순채무(원) (SETTLEMENT_PLAN 4.4)")
    available: int = Field(description="더 보낼 수 있는 금액(원)")


class ReserveAdmin(ReserveSummary):
    updated_at: datetime | None = Field(description="마지막 변동 시각. 계좌가 아직 없으면 null")
    status: str = Field(description="OK(잔액 ≥ 0) / OVERDRAWN(−한도 ≤ 잔액 < 0) / OVERDUE(잔액 < −한도). 관리자용")


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


# --- 입금·인출 신청 (SPEC 10.9) ---

MAX_REQUEST_AMOUNT = 1_000_000_000_000  # 1조 원. 오타(0 하나 더)를 막는 정도의 상한


class ReserveRequestIn(BaseModel):
    """은행의 신청. request_ref는 은행이 정하는 신청 번호 (같은 은행 안에서 유일, 다시 보내도 한 건)."""

    request_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$",
                             description="은행이 정하는 신청 번호 (영문·숫자·_ . : -)")
    kind: Literal["DEPOSIT", "WITHDRAW"]
    amount: int = Field(ge=1, le=MAX_REQUEST_AMOUNT, description="금액(원)")
    memo: str = Field(default="", max_length=200)


class ReserveRequestOut(BaseModel):
    request_id: int
    request_ref: str
    kind: str
    amount: int
    memo: str
    status: str = Field(description="PENDING / APPROVED / REJECTED / CANCELLED")
    reject_reason: str | None
    entry_id: int | None = Field(description="승인 때 남긴 예치금 분개 번호")
    balance_after: int | None = Field(description="승인 분개 반영 뒤 예치금 잔액")
    created_at: datetime
    decided_at: datetime | None

    @field_serializer("created_at", "decided_at")
    def _times(self, value: datetime | None) -> str | None:
        return spec_time(value)


class ReserveRequestList(BaseModel):
    requests: list[ReserveRequestOut]


class ReserveRequestAdminOut(ReserveRequestOut):
    bank_code: str
    decided_by: str | None


class ReserveRequestReject(BaseModel):
    reason: str = Field(min_length=1, max_length=200, description="거절 사유 (은행에 그대로 보인다)")

    @field_validator("reason")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("거절 사유를 넣어야 합니다")
        return value.strip()


class ReserveRequestApprove(BaseModel):
    memo: str = Field(default="", max_length=100, description="분개 메모에 덧붙일 관리자 메모")
