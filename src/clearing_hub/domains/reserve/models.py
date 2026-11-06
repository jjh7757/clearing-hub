"""예치금 원장 (SETTLEMENT_PLAN 2절 5번, 4.1). 한국은행 당좌계정 역할의 모의 예치금이다. 실제 돈은 움직이지 않는다.

거래 기록(`transfers`)과는 다른 장부다. 이 도메인은 `transfers`·`settlement_*`를 모르고, 정산 결과는
정산 지시서 `(run_id, [(bank_code, net_amount)])`로만 받는다 (service.apply_instruction).
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from clearing_hub.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ReserveLedgerKind(StrEnum):
    DEPOSIT = "DEPOSIT"  # 관리자 예치 (+)
    WITHDRAW = "WITHDRAW"  # 관리자 인출 (−). 잔액 − 금액 ≥ −순채무한도일 때만
    SETTLE_CREDIT = "SETTLE_CREDIT"  # 정산에서 받을 돈 (+)
    SETTLE_DEBIT = "SETTLE_DEBIT"  # 정산에서 낼 돈 (−)
    ADJUST = "ADJUST"  # 관리자 정정 (±, 사유 필수). 정산된 거래는 고치지 않고 이것으로만 정정한다 (2절 8번)


class PositionStatus(StrEnum):
    """정산 반영 뒤 은행 포지션의 결과 (SETTLEMENT_PLAN 2절 7번, 5.1의 12단계)."""

    OK = "OK"  # 잔액 ≥ 0
    OVERDRAWN = "OVERDRAWN"  # −순채무한도 ≤ 잔액 < 0: 한도 안의 마이너스
    OVERDUE = "OVERDUE"  # 잔액 < −순채무한도: 그래도 반영하고, 송금은 순채무한도 검사에 걸려 막힌다 (2단계)


class ReserveAccount(Base):
    """은행별 예치금 계좌. 잔액은 분개(reserve_ledger)와 같은 트랜잭션에서만 바뀐다."""

    __tablename__ = "reserve_accounts"

    bank_code: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)  # 원. 마이너스 가능
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class ReserveLedgerEntry(Base):
    """예치금 분개: 모든 잔액 변동의 근거. 고치거나 지우지 않는다."""

    __tablename__ = "reserve_ledger"
    # 같은 정산을 두 번 반영하지 못하게. run_id가 NULL인 입출금·정정 분개는 SQLite·PostgreSQL 모두 검사에서 빠진다
    __table_args__ = (UniqueConstraint("run_id", "bank_code", "kind", name="uq_reserve_ledger_run"),)

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_code: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # ReserveLedgerKind
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 잔액 증감. WITHDRAW·SETTLE_DEBIT는 음수
    # 정산 회차 (SETTLE_*만). settlement_runs 테이블은 3단계에서 생기므로 지금은 FK가 없다.
    # TODO 3단계: ForeignKey("settlement_runs.run_id")를 붙이는 마이그레이션
    run_id: Mapped[int | None] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 이 분개를 반영한 뒤 잔액
    memo: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(50), nullable=False)  # "admin" / "settlement"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
