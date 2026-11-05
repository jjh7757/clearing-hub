from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl

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


class BankAdmin(BankPublic):
    base_url: str
    active: bool
    created_at: datetime


class BankCredentials(BankAdmin):
    """등록·키 재발급 응답. api_key와 secret은 이때 한 번만 보여 준다."""

    api_key: str
    secret: str
