"""연동 진단 (SETTLEMENT_PLAN 14절): 인증 실패 기록 `auth_failures`."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from clearing_hub.core.database import Base


class FailureDirection(StrEnum):
    BANK_TO_HUB = "BANK_TO_HUB"  # 은행 요청을 허브가 401로 거절 (core/auth.current_bank)
    HUB_TO_BANK = "HUB_TO_BANK"  # 허브 요청을 은행이 401·403으로 거절했거나, 은행 응답 서명이 틀림 (core/bank_client)


class AuthFailure(Base):
    """인증 실패 한 건 (규칙 진단기 core/signature_doctor.py의 입력). 로그 표라 FK를 걸지 않는다.

    서명을 다시 계산해 보려면 받은(보낸) 바이트가 그대로 있어야 하므로 body는 원문 그대로 둔다 (64KB까지, 넘으면 앞부분과
    body_truncated). headers_json에는 서명 관련 헤더와 X-Request-ID만 넣는다. **X-Api-Key는 저장하지 않는다**
    (보냈는지만 api_key_sent). bank_code는 API 키가 맞은 은행일 때만 채운다: 키가 틀린 요청은 누구의 것인지 모르므로
    은행 API(/v1/diagnose/failures)로는 아무도 못 보고, 관리자만 claimed_bank_code로 본다."""

    __tablename__ = "auth_failures"
    __table_args__ = (Index("ix_auth_failures_bank", "bank_code", "failure_id"),
                      Index("ix_auth_failures_claimed", "claimed_bank_code", "failure_id"))

    failure_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)
    claimed_bank_code: Mapped[str | None] = mapped_column(String(20))  # BANK_TO_HUB의 X-Bank-Code 헤더 값 (없을 수 있다)
    bank_code: Mapped[str | None] = mapped_column(String(3))  # 식별된 은행. HUB_TO_BANK는 부른 은행
    reason: Mapped[str] = mapped_column(String(40), nullable=False)  # 오류 코드 (HUB_TO_BANK는 은행이 준 코드 또는 RESPONSE_SIGNATURE_INVALID)
    message: Mapped[str | None] = mapped_column(String(300))  # 허브가 준(은행이 준) 설명
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)  # 서명에 쓰는 규격 경로 (BANK_TO_HUB는 받은 경로)
    query: Mapped[str | None] = mapped_column(String(300))  # 받은 쿼리스트링 (있으면)
    url: Mapped[str | None] = mapped_column(String(500))  # 받은 전체 URL(쿼리 제외) / 허브가 부른 주소
    headers_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    api_key_sent: Mapped[bool | None] = mapped_column(Boolean)  # BANK_TO_HUB: X-Api-Key 헤더가 있었는가 (값은 저장 안 함)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # HUB_TO_BANK: 은행 응답
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_headers_json: Mapped[dict | None] = mapped_column(JSON)
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    response_signature_valid: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

