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
    # 9단계 (SETTLEMENT_PLAN 13절): 매일 이 시각(HH:MM, settlement_timezone)에 dispute 없는 DRAFT를 확정한다.
    # 그 전에 포지션이 있는 은행이 모두 ack하면 그 자리에서 확정한다 (settlement/auto_confirm.py)
    settlement_confirm_deadline: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # 셀프 시험 (SPEC 3.7): 허브가 이 은행(가짜 은행) 명의로 시험 이체를 만든다. 은행별 하루(settlement_timezone) 횟수
    sim_bank_code: str = Field(default="902", pattern=r"^\d{3}$")
    self_test_daily_limit: int = Field(default=20, gt=0)
    # 알림 웹훅 (core/alerts.py). Slack·Discord의 incoming webhook 주소. 비우면 알림을 보내지 않는다 (로그만)
    alert_webhook_url: str | None = None
    # 부인방지 (SETTLEMENT_PLAN 12절, SPEC 2절). 허브 Ed25519 개인키(raw 32바이트 base64, 44자). **필수**:
    # 허브는 모든 은행 호출에 이 키로 서명하고 /v1 응답에도 서명한다. 없거나 형식이 틀리면 서버가 시작하지 않는다
    hub_signing_key: SecretStr
    previous_key_ttl_seconds: int = Field(default=86400, gt=0)  # 은행 공개키 교체 뒤 이전 키를 받아 주는 시간 (12.2 8번)
    # 셀프 등록·기본 한도 (SETTLEMENT_PLAN 12.11, SPEC 3.5·7절·10.2)
    invite_ttl_days: int = Field(default=7, gt=0)  # 초대 코드 유효 기간
    bank_code_start: str = Field(default="903", pattern=r"^\d{3}$")  # 셀프 등록 때 이 코드부터 비어 있는 가장 작은 코드를 준다
    default_net_debit_cap: int = Field(default=1_000_000_000, ge=0)  # 새 은행의 순채무한도(원). 등록 직후부터 보낼 수 있게
    # 은행이 부를 허브 주소 (초대 문구에 넣는다). 없으면 그 관리자 요청의 base URL. 프록시 뒤에서는 내부 주소가 나올 수 있어 운영에서는 넣는다
    hub_public_url: str | None = None
    # 10단계 연동 지원 (SETTLEMENT_PLAN 14절, SPEC 3.8). 서명 진단은 규칙(core/signature_doctor.py)이 하고, LLM은 그 결과를
    # 한국어로 설명만 한다 (읽기 전용). 키가 없으면 LLM 단계를 건너뛰고 규칙 결과만 돌려준다
    gemini_api_key: SecretStr | None = None  # AvengersBank와 같은 Gemini. 없으면 LLM 단계를 건너뛴다
    diagnose_model: str = "gemini-2.5-flash"  # 설명용 모델 (core/llm.py)
    diagnose_max_tokens: int = Field(default=2048, gt=0)  # 10줄 안팎의 설명
    diagnose_timeout_seconds: float = Field(default=30, gt=0)  # 넘으면 규칙 결과만
    diagnose_daily_limit: int = Field(default=50, gt=0)  # POST /v1/diagnose: 은행별 하루(settlement_timezone) 횟수
    spec_path: str = "docs/SPEC.md"  # SPEC 발췌용 (작업 디렉터리 기준. 없으면 저장소 루트의 docs/SPEC.md)

    @field_validator("hub_signing_key", mode="before")
    @classmethod
    def _check_hub_key(cls, value):
        """빈 값·형식이 틀린 키는 서버 시작 때 바로 알린다 (첫 이체에서 터지지 않게)."""
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if raw is None or not str(raw).strip():
            raise ValueError("HUB_SIGNING_KEY(허브 Ed25519 개인키)가 필요합니다. docs/DEPLOY.md '허브 키 만들기'")
        from clearing_hub.core.signing import public_key_of  # signing이 config를 import하지 않으므로 순환 없음

        public_key_of(str(raw).strip())  # 틀리면 ValueError → 설정 오류
        return str(raw).strip()

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
