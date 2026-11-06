from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_serializer, field_validator, model_validator

from clearing_hub.core.signing import decode_public_key

BANK_CODE = Field(pattern=r"^\d{3}$", description="은행 코드 3자리")
PUBLIC_KEY = Field(description="Ed25519 공개키: raw 32바이트의 표준 base64 (패딩 포함 44자)")


def _spec_time(value: datetime | None) -> str | None:
    """SPEC 1절 시각 형식. transfers.schemas.spec_time과 같다 (그쪽이 이 모듈을 import하므로 따로 둔다). SQLite는 시간대 없이 준다."""
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_public_key(value: str) -> str:
    if decode_public_key(value) is None:
        raise ValueError("public_key는 raw 32바이트 Ed25519 공개키의 base64(44자)여야 합니다")
    return value


class BankPublic(BaseModel):
    bank_code: str
    name: str


class BankList(BaseModel):
    banks: list[BankPublic]


class BankCreate(BaseModel):
    """관리자 직접 등록. 셀프 등록(POST /v1/banks/register)을 쓸 수 없을 때만 쓴다 (시연용 코드 등)."""

    bank_code: str = BANK_CODE
    name: str = Field(min_length=1, max_length=50)
    base_url: HttpUrl = Field(description="은행의 /interbank 주소")
    public_key: str = PUBLIC_KEY
    net_debit_cap: int | None = Field(default=None, ge=0, description="순채무한도(원). 빼면 DEFAULT_NET_DEBIT_CAP")

    @field_validator("public_key")
    @classmethod
    def _ed25519_public_key(cls, value: str) -> str:
        return _check_public_key(value)


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
    public_key_fingerprint: str | None = None  # 등록된 Ed25519 공개키의 지문 (sha256 앞 16자). NULL이면 이 은행은 아직 쓸 수 없다


class BankCredentials(BankAdmin):
    """관리자 등록·API 키 재발급 응답. api_key는 이때 한 번만 보여 준다."""

    api_key: str


class BankMe(BaseModel):
    """GET·PATCH /v1/me 응답: 내 등록 정보 (SPEC 3.6)."""

    bank_code: str
    name: str
    base_url: str
    active: bool
    net_debit_cap: int
    public_key_fingerprint: str | None
    created_at: datetime

    @field_serializer("created_at")
    def _time(self, value: datetime) -> str | None:
        return _spec_time(value)


class MeUpdate(BaseModel):
    """PATCH /v1/me. 넣은 필드만 바꾼다. 코드·키·한도는 바꾸지 않는다 (키는 PUT /v1/keys, 한도는 허브 관리자)."""

    name: str | None = Field(default=None, min_length=1, max_length=50)
    base_url: HttpUrl | None = Field(default=None, description="은행의 /interbank 주소. 바꾸면 허브가 새 주소로 ping한다")

    @model_validator(mode="after")
    def something_to_change(self) -> "MeUpdate":
        if self.name is None and self.base_url is None:
            raise ValueError("name, base_url 중 하나 이상을 넣어야 합니다")
        return self


class PingResult(BaseModel):
    """은행 연결 확인 결과 (SPEC 3.6). 허브가 없는 계좌로 {base_url}/inquiry를 허브 키로 서명해 부른다."""

    ok: bool  # 연결·서명 검증·응답 서명 모두 정상 (status 200, 또는 404 ACCOUNT_NOT_FOUND, 응답 서명 맞음)
    reachable: bool  # 응답을 받았는지 (False면 주소·방화벽·인증서·5초 타임아웃)
    status: int | None  # 받은 HTTP 상태 그대로
    body: dict  # 받은 본문 (JSON 객체가 아니면 {})
    response_signature_valid: bool | None  # 응답 서명(SPEC 2.5)이 맞는지. 응답이 없으면 None


# ---- 셀프 등록 (SETTLEMENT_PLAN 12.11, SPEC 3.5) ----

class InviteCreate(BaseModel):
    note: str = Field(default="", max_length=100, description="팀 이름 등 메모")


class InviteIssued(BaseModel):
    """발급 응답. code는 이때만 보여 준다."""

    code: str
    note: str
    expires_at: datetime

    @field_serializer("expires_at")
    def _time(self, value: datetime) -> str | None:
        return _spec_time(value)


class InviteAdmin(BaseModel):
    """관리자 목록. 코드는 앞 8자만 (이미 전달한 코드를 화면에서 다시 꺼내 쓰지 않게)."""

    code_prefix: str
    note: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    used_by_bank_code: str | None
    status: Literal["UNUSED", "USED", "EXPIRED"]

    @field_serializer("created_at", "expires_at", "used_at")
    def _time(self, value: datetime | None) -> str | None:
        return _spec_time(value)


class BankRegister(BaseModel):
    """POST /v1/banks/register (인증 없음). 은행 코드는 허브가 정한다."""

    invite_code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=50)
    base_url: HttpUrl = Field(description="은행의 /interbank 주소 (공개 HTTPS)")
    public_key: str = PUBLIC_KEY

    @field_validator("public_key")
    @classmethod
    def _ed25519_public_key(cls, value: str) -> str:
        return _check_public_key(value)


class BankRegistered(BaseModel):
    bank_code: str
    name: str
    api_key: str  # 이때 한 번만 보여 준다
    hub_public_key: str
    hub_fingerprint: str
    net_debit_cap: int
    ping: PingResult


# ---- 공개키 (SETTLEMENT_PLAN 12.5, SPEC 2.6) ----

class PublicKeyIn(BaseModel):
    public_key: str = PUBLIC_KEY

    @field_validator("public_key")
    @classmethod
    def _ed25519_public_key(cls, value: str) -> str:
        return _check_public_key(value)


class KeyStatus(BaseModel):
    """PUT·GET /v1/keys 응답. 시각은 SPEC 1절 형식."""

    bank_code: str
    fingerprint: str | None
    registered_at: datetime | None
    previous_fingerprint: str | None = None
    previous_key_expires_at: datetime | None
    hub_fingerprint: str | None = None  # 허브 공개키 지문. GET /v1/hub-key로 받은 키를 이 값과 맞춰 본다

    @field_serializer("registered_at", "previous_key_expires_at")
    def _time(self, value: datetime | None) -> str | None:
        return _spec_time(value)


class HubKey(BaseModel):
    algorithm: Literal["ed25519"] = "ed25519"
    public_key: str
    fingerprint: str
