from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from clearing_hub.domains.banks.schemas import BANK_CODE


class TransferRequest(BaseModel):
    """SPEC 3.3. from_bank는 본문이 아니라 인증된 X-Bank-Code로 정한다."""

    transfer_id: str = Field(description="UUID v4, 소문자. 멱등성 키")
    to_bank: str = BANK_CODE
    to_account: str | None = Field(default=None, min_length=1, max_length=50)
    to_account_ref: str | None = Field(default=None, min_length=1, max_length=64, description="본인 계좌 조회(SPEC 9.3)로 받은 참조값")
    from_account: str = Field(min_length=1, max_length=50)
    amount: int = Field(gt=0, strict=True, description="정수, 원")
    sender_name: str = Field(min_length=1, max_length=50)
    memo: str = Field(default="", max_length=100)

    @field_validator("transfer_id")
    @classmethod
    def uuid_lower(cls, v: str) -> str:
        """SPEC 1절: UUID v4, 소문자, 하이픈 포함 36자. 형식이 틀리면 UUID()가 ValueError를 낸다 (422)."""
        parsed = UUID(v)
        if str(parsed) != v or parsed.version != 4:
            raise ValueError("transfer_id는 소문자 UUID v4여야 합니다")
        return v

    @model_validator(mode="after")
    def one_destination(self) -> "TransferRequest":
        if (self.to_account is None) == (self.to_account_ref is None):
            raise ValueError("to_account와 to_account_ref 중 하나만 넣어야 합니다")
        return self


class SelfTestRequest(BaseModel):
    """POST /v1/test-receive 본문 (SPEC 3.7). 보내는 쪽 정보는 허브가 채운다 (가짜 은행 902, 시험 계좌)."""

    to_account: str = Field(min_length=1, max_length=50, description="내 은행의 시험 받을 계좌")
    amount: int = Field(ge=1, le=100_000, strict=True, description="정수, 원. 1~100,000")


class TransferResolve(BaseModel):
    """관리자가 UNKNOWN 거래를 직접 확정한다. 받는 은행과 확인한 결과만 넣는다."""

    status: Literal["COMPLETED", "FAILED"]
    reason: str = Field(min_length=1, max_length=200, description="확인 방법·근거 (이력에만 남는다)")
    holder_name: str | None = Field(default=None, min_length=1, max_length=50, description="COMPLETED일 때 예금주 (선택)")


def spec_time(value: datetime | None) -> str | None:
    """SPEC 1절 시각 형식: ISO 8601, UTC, 초 단위 ("2026-11-05T01:20:00Z"). SQLite는 시간대 없이 주므로 UTC로 본다."""
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class TransferResult(BaseModel):
    transfer_id: str
    status: str
    holder_name: str | None = None
    finished_at: datetime | None = None

    @field_serializer("finished_at")
    def _finished_at(self, value: datetime | None) -> str | None:
        return spec_time(value)


class TransferDetail(TransferResult):
    to_bank: str
    amount: int
    requested_at: datetime
    fail_code: str | None = None
    fail_message: str | None = None

    @field_serializer("requested_at")
    def _requested_at(self, value: datetime) -> str | None:
        return spec_time(value)
