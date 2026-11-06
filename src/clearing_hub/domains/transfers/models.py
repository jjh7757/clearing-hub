from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from clearing_hub.core.database import Base


class TransferStatus(StrEnum):
    ACCEPTED = "ACCEPTED"  # 접수·저장됨
    DELIVERING = "DELIVERING"  # 받는 은행 호출 중
    COMPLETED = "COMPLETED"  # 받는 은행이 입금 확인 (정산 대상)
    FAILED = "FAILED"  # 받는 은행이 거절. 보내는 은행이 출금 되돌림
    PENDING = "PENDING"  # 응답 없음. 재시도 태스크가 확인
    UNKNOWN = "UNKNOWN"  # 재시도를 다 했는데도 모름. 관리자 확인


class Transfer(Base):
    __tablename__ = "transfers"
    # 미정산 순채무 계산(transfers/exposure.py)과 정산 수집용 (SETTLEMENT_PLAN 4.2, 10절)
    __table_args__ = (
        Index("ix_transfers_from_bank_settlement", "from_bank", "settlement_run_id"),
        Index("ix_transfers_to_bank_settlement", "to_bank", "settlement_run_id"),
    )

    transfer_id: Mapped[str] = mapped_column(String(36), primary_key=True)  # 보내는 은행이 만든 UUID (멱등성 키)
    from_bank: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), index=True, nullable=False)
    from_account: Mapped[str] = mapped_column(String(50), nullable=False)
    to_bank: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), index=True, nullable=False)
    to_account: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_name: Mapped[str] = mapped_column(String(50), nullable=False)
    memo: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    holder_name: Mapped[str | None] = mapped_column(String(50))  # 받는 은행이 알려 준 예금주
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    fail_code: Mapped[str | None] = mapped_column(String(40))
    fail_message: Mapped[str | None] = mapped_column(String(200))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))  # 접수 때의 X-Request-ID. 재시도 호출에도 같은 값 (SPEC 2.4)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 받는 은행에 처음 보낸 시각
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # COMPLETED/FAILED 확정 시각
    # 이 거래를 모은 정산 회차 (SETTLEMENT_PLAN 4.2). 채워지면 상태를 바꾸지 않는다 (2절 8번, service._set_status).
    # DRAFT를 취소하면 NULL로 돌아간다
    settlement_run_id: Mapped[int | None] = mapped_column(ForeignKey("settlement_runs.run_id"))


class TransferEvent(Base):
    """상태 변경 이력 (감사·관리 화면 타임라인)."""

    __tablename__ = "transfer_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.transfer_id"), index=True, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EvidenceDirection(StrEnum):
    REQUEST_IN = "REQUEST_IN"  # 은행 → 허브 이체 접수 요청 (POST /v1/transfers)
    RECEIVE_OUT = "RECEIVE_OUT"  # 허브 → 받는 은행 요청 (POST /receive, GET /receive/{id})
    RECEIVE_REPLY = "RECEIVE_REPLY"  # 받는 은행의 응답


class TransferEvidence(Base):
    """분쟁 때 꺼내 보일 원문 (SETTLEMENT_PLAN 12.2 9번·12.3). 고치거나 지우지 않는다 (추가만 한다).

    서명은 원본 바이트에 대한 것이라 body는 받은·보낸 바이트 그대로 둔다. headers_json에는 서명 검증에 필요한 헤더만
    넣는다 (X-Api-Key는 넣지 않는다). 다시 검증하려면 method·path(요청)나 http_status와 짝 요청의 X-Signature(응답)가
    필요하므로 칸을 따로 둔다."""

    __tablename__ = "transfer_evidence"

    evidence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.transfer_id"), index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    method: Mapped[str | None] = mapped_column(String(10))  # 요청만
    path: Mapped[str | None] = mapped_column(String(200))  # 서명에 쓴 규격 경로 (요청만)
    http_status: Mapped[int | None] = mapped_column(Integer)  # 응답만
    headers_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    signature_alg: Mapped[str | None] = mapped_column(String(10))  # ed25519 | None(서명 없음). 8c 전 행에는 hmac이 있을 수 있다
    # 받을 때 검증 결과. 허브가 보낸 요청(RECEIVE_OUT)은 허브가 서명했으므로 None. 응답 서명이 없고 요구하지도 않으면 None
    signature_valid: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
