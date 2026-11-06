from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_serializer, field_validator, model_validator

from clearing_hub.core.signing import decode_public_key

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
    signature_mode: str  # hmac | both | ed25519 (SETTLEMENT_PLAN 12.2 7번)
    public_key_fingerprint: str | None = None  # 등록된 Ed25519 공개키의 지문 (sha256 앞 16자)


class BankCredentials(BankAdmin):
    """등록·키 재발급 응답. api_key와 secret은 이때 한 번만 보여 준다."""

    api_key: str
    secret: str


# ---- 공개키·서명 모드 (SETTLEMENT_PLAN 12.5, SPEC 2.6) ----

class PublicKeyIn(BaseModel):
    public_key: str = Field(description="Ed25519 공개키: raw 32바이트의 표준 base64 (패딩 포함 44자)")

    @field_validator("public_key")
    @classmethod
    def _ed25519_public_key(cls, value: str) -> str:
        if decode_public_key(value) is None:
            raise ValueError("public_key는 raw 32바이트 Ed25519 공개키의 base64(44자)여야 합니다")
        return value


class SignatureModeIn(BaseModel):
    signature_mode: Literal["hmac", "both", "ed25519"]


class KeyStatus(BaseModel):
    """PUT·GET /v1/keys 응답. 시각은 SPEC 1절 형식."""

    bank_code: str
    fingerprint: str | None
    signature_mode: str
    registered_at: datetime | None
    previous_fingerprint: str | None = None
    previous_key_expires_at: datetime | None
    hub_fingerprint: str | None = None  # 허브 공개키 지문. GET /v1/hub-key로 받은 키를 이 값과 맞춰 본다

    @field_serializer("registered_at", "previous_key_expires_at")
    def _time(self, value: datetime | None) -> str | None:
        # transfers.schemas.spec_time과 같다 (그쪽이 이 모듈을 import하므로 따로 둔다). SQLite는 시간대 없이 준다
        if value is None:
            return None
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class HubKey(BaseModel):
    algorithm: Literal["ed25519"] = "ed25519"
    public_key: str
    fingerprint: str
