from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
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


class TransferEvent(Base):
    """상태 변경 이력 (감사·관리 화면 타임라인)."""

    __tablename__ = "transfer_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("transfers.transfer_id"), index=True, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
