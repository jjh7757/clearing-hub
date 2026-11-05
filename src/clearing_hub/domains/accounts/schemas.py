from typing import Literal

from pydantic import BaseModel, Field


class LookupRequest(BaseModel):
    ci: str = Field(pattern=r"^[0-9a-f]{64}$", description="가상 CI (SPEC 9.1), 소문자 16진수 64자")
    consent_id: str = Field(min_length=1, max_length=64, description="보내는 은행이 저장한 동의 기록 ID")


class FoundAccount(BaseModel):
    bank_code: str
    bank_name: str
    account_masked: str
    product_name: str
    account_ref: str


class BankLookupResult(BaseModel):
    bank_code: str
    result: Literal["OK", "UNSUPPORTED", "TIMEOUT", "ERROR"]
    count: int | None = None


class LookupResponse(BaseModel):
    lookup_id: str
    accounts: list[FoundAccount]
    banks: list[BankLookupResult]
