"""정산 배치 (SETTLEMENT_PLAN 4.1, 5.1의 8·9단계). 거래(`transfers`)를 모아 은행별 차액을 계산한 결과를 남긴다.

예치금 반영(11·12단계)은 이 도메인이 정산 지시서를 만들어 `reserve.apply_instruction`에 넘긴다 (service.confirm).
`reserve`는 이 테이블을 읽지 않는다 (2절 1번).
"""

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clearing_hub.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class SettlementStatus(StrEnum):
    DRAFT = "DRAFT"  # 수집·상계만 함 (8·9단계). 예치금은 아직 그대로다. 동시에 하나만 있을 수 있다
    SETTLED = "SETTLED"  # 예치금에 반영됨 (11·12단계, service.confirm). 거래는 더 바꾸지 않는다 (2절 8번)
    CANCELLED = "CANCELLED"  # DRAFT를 취소함. 거래의 settlement_run_id는 NULL로 돌아가 다음 run에 다시 들어간다


class PairDirection(StrEnum):
    A_TO_B = "a_to_b"  # bank_a가 bank_b에게 net_amount를 낸다
    B_TO_A = "b_to_a"
    NONE = "none"  # 서로 같은 금액을 보냈다


class SettlementRun(Base):
    """정산 1회. 한 날짜에 여러 번 돌릴 수 있다(시연용 즉시 마감). 대상은 날짜가 아니라 '미정산' 거래다 (2절 3번)."""

    __tablename__ = "settlement_runs"
    # DRAFT는 동시에 하나만: 서비스가 먼저 확인하고(409), 동시 실행으로 그걸 지나치면 이 부분 유니크 인덱스가 막는다
    __table_args__ = (
        Index("uq_settlement_runs_single_draft", "status", unique=True,
              postgresql_where=text("status = 'DRAFT'"), sqlite_where=text("status = 'DRAFT'")),
    )

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    settle_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)  # 정산일 (settlement_timezone 기준)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 이 시각 전에 끝난 거래까지
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SettlementStatus
    tx_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # 모은 거래 금액의 합
    created_by: Mapped[str] = mapped_column(String(50), nullable=False)  # "admin" / "auto" (workers/settlement.py)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # service.confirm
    settled_by: Mapped[str | None] = mapped_column(String(50))  # service.confirm: 관리자("admin") / "auto"

    # 비동기 세션에서 지연 로딩은 쓸 수 없으므로 "raise": 읽을 때는 selectinload로 함께 읽는다 (service.get_run)
    positions: Mapped[list["SettlementPosition"]] = relationship(lazy="raise", order_by="SettlementPosition.bank_code")
    pairs: Mapped[list["SettlementPair"]] = relationship(
        lazy="raise", order_by="[SettlementPair.bank_a, SettlementPair.bank_b]")


class SettlementPosition(Base):
    """은행별 순 포지션 (9단계 결과, 11단계 정산 지시서). 그 run의 거래에 나타난 은행만 행이 있다."""

    __tablename__ = "settlement_positions"

    run_id: Mapped[int] = mapped_column(ForeignKey("settlement_runs.run_id"), primary_key=True)
    bank_code: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), primary_key=True)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_total: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 이 은행이 보낸 것 (줄 돈)
    recv_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recv_total: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 이 은행이 받은 것 (받을 돈)
    net_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # recv_total − sent_total. 양수면 받는다
    # confirm 뒤 채운다: reserve.models.PositionStatus 값(OK / OVERDRAWN / OVERDUE)과 반영 뒤 예치금 잔액
    result: Mapped[str | None] = mapped_column(String(20))
    balance_after: Mapped[int | None] = mapped_column(BigInteger)


class SettlementPair(Base):
    """은행 쌍별 표 (화면·CSV용, 계산에는 안 쓴다 — 2절 2번). bank_a < bank_b로 한 쌍에 한 행."""

    __tablename__ = "settlement_pairs"
    __table_args__ = (CheckConstraint("bank_a < bank_b", name="ck_settlement_pairs_order"),)

    run_id: Mapped[int] = mapped_column(ForeignKey("settlement_runs.run_id"), primary_key=True)
    bank_a: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), primary_key=True)
    bank_b: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), primary_key=True)
    a_to_b_count: Mapped[int] = mapped_column(Integer, nullable=False)
    a_to_b_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    b_to_a_count: Mapped[int] = mapped_column(Integer, nullable=False)
    b_to_a_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # |a_to_b_total − b_to_a_total|
    net_direction: Mapped[str] = mapped_column(String(10), nullable=False)  # PairDirection
