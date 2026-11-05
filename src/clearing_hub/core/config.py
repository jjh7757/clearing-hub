from functools import lru_cache

from pydantic import Field, SecretStr
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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
