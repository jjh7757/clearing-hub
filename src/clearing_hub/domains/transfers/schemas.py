from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from clearing_hub.domains.banks.schemas import BANK_CODE


class TransferRequest(BaseModel):
    """SPEC 3.3. from_bank는 본문이 아니라 인증된 X-Bank-Code로 정한다."""

    transfer_id: str = Field(description="UUID v4, 소문자. 멱등성 키")
    to_bank: str = BANK_CODE
    to_account: str = Field(min_length=1, max_length=50)
    from_account: str = Field(min_length=1, max_length=50)
    amount: int = Field(gt=0, strict=True, description="정수, 원")
    sender_name: str = Field(min_length=1, max_length=50)
    memo: str = Field(default="", max_length=100)

    @field_validator("transfer_id")
    @classmethod
    def uuid_lower(cls, v: str) -> str:
        if str(UUID(v)) != v:
            raise ValueError("transfer_id는 소문자 UUID여야 합니다")
        return v


class TransferResult(BaseModel):
    transfer_id: str
    status: str
    holder_name: str | None = None
    finished_at: datetime | None = None


class TransferDetail(TransferResult):
    to_bank: str
    amount: int
    requested_at: datetime
    fail_code: str | None = None
    fail_message: str | None = None
