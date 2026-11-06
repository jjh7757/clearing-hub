from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, model_validator

BANK_CODE = Field(pattern=r"^\d{3}$", description="은행 코드 3자리")


class BankPublic(BaseModel):
    bank_code: str
    name: str


class BankList(BaseModel):
    banks: list[BankPublic]


class BankCreate(BaseModel):
    bank_code: str = BANK_CODE
    name: str = Field(min_length=1, max_length=50)
    base_url: HttpUrl = Field(description="은행의 /interbank 주소")


class BankUpdate(BaseModel):
    """은행 정보 변경. 넣은 필드만 바꾼다. 은행 코드·키는 바꾸지 않는다 (키는 /keys)."""

    name: str | None = Field(default=None, min_length=1, max_length=50)
    base_url: HttpUrl | None = Field(default=None, description="은행의 /interbank 주소")
    net_debit_cap: int | None = Field(default=None, ge=0, description="순채무한도(원) (SETTLEMENT_PLAN 2절 6번)")

    @model_validator(mode="after")
    def something_to_change(self) -> "BankUpdate":
        if self.name is None and self.base_url is None and self.net_debit_cap is None:
            raise ValueError("name, base_url, net_debit_cap 중 하나 이상을 넣어야 합니다")
        return self


class BankAdmin(BankPublic):
    base_url: str
    active: bool
    net_debit_cap: int
    created_at: datetime


class BankCredentials(BankAdmin):
    """등록·키 재발급 응답. api_key와 secret은 이때 한 번만 보여 준다."""

    api_key: str
    secret: str
