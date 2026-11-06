from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(min_length=1)
    admin_token: SecretStr = Field(min_length=32)  # 관리 API 헤더 X-Admin-Token (관리자 JWT는 다음 단계)
    db_echo: bool = False
    signature_max_skew_seconds: int = Field(default=300, gt=0)  # 서명 시각 허용 오차 (SPEC 2.2)
    receiver_timeout_seconds: float = Field(default=5, gt=0)  # 허브 → 은행 호출 타임아웃 (SPEC 1)
    retry_interval_seconds: float = Field(default=60, gt=0)  # 미확인 거래 확인 간격
    retry_max_count: int = Field(default=10, gt=0)  # 이 횟수 뒤에도 모르면 UNKNOWN
    max_transfer_amount: int = Field(default=100_000_000, gt=0)  # 1회 상한(원)
    lookup_daily_limit: int = Field(default=10, gt=0)  # 본인 계좌 조회: 은행별 같은 CI 24시간 횟수 (SPEC 9.2)
    account_ref_ttl_seconds: int = Field(default=1800, gt=0)  # account_ref 유효 시간 (SPEC 9.3)
    # 정산 (SETTLEMENT_PLAN 2절 3·4번, 5.1). 일일 배치는 workers/settlement.py
    settlement_worker_enabled: bool = True  # false면 일일 배치 태스크를 띄우지 않는다 (테스트, 수동 운영)
    settlement_cutoff_time: str = Field(default="00:05", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # 매일 마감 배치 시각 (HH:MM)
    settlement_timezone: str = "Asia/Seoul"  # 정산일·마감 시각의 기준 시간대
    settlement_auto_confirm: bool = False  # true면 DRAFT를 대사 없이 바로 확정 (시연용)
    # 부인방지 (SETTLEMENT_PLAN 12절, SPEC 2.3·2.5·2.6). 허브 Ed25519 개인키(raw 32바이트 base64).
    # 없으면 허브는 Ed25519로 보내지도(ed25519 모드 전환 불가, HUB_KEY_MISSING) 응답에 서명하지도 못한다. 받는 것(both)은 된다
    hub_signing_key: SecretStr | None = None
    previous_key_ttl_seconds: int = Field(default=86400, gt=0)  # 은행 공개키 교체 뒤 이전 키를 받아 주는 시간 (12.2 8번)

    @field_validator("hub_signing_key", mode="before")
    @classmethod
    def _check_hub_key(cls, value):
        """빈 값은 키 없음. 형식이 틀린 키는 서버 시작 때 바로 알린다 (첫 이체에서 터지지 않게)."""
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if raw is None or not str(raw).strip():
            return None
        from clearing_hub.core.signing import public_key_of  # signing이 config를 import하지 않으므로 순환 없음

        public_key_of(str(raw).strip())  # 틀리면 ValueError → 설정 오류
        return str(raw).strip()

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
