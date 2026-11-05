"""본인 명의 계좌 조회 (SPEC 9절). ci 원문은 저장하지 않는다 (9.5)."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from clearing_hub.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class AccountLookup(Base):
    """조회 기록: 누가, 누구의 동의(consent_id)로, 언제, 어느 은행에서 몇 개를 받았는지."""

    __tablename__ = "account_lookups"

    lookup_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    from_bank: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), index=True, nullable=False)
    consent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256(ci). 횟수 제한에만 쓰고 24시간 뒤 지운다 (purge_expired)
    ci_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    results: Mapped[str] = mapped_column(Text, nullable=False)  # 은행별 결과 JSON (SPEC 9.2의 banks)
    account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True, nullable=False)


class AccountRef(Base):
    """account_ref ↔ 실제 계좌번호. 조회한 은행만, 만료 전까지 이체에 쓸 수 있다 (SPEC 9.3)."""

    __tablename__ = "account_refs"

    ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    lookup_id: Mapped[str] = mapped_column(ForeignKey("account_lookups.lookup_id"), index=True, nullable=False)
    owner_bank: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), nullable=False)  # 조회한 은행
    bank_code: Mapped[str] = mapped_column(ForeignKey("banks.bank_code"), nullable=False)  # 계좌가 있는 은행
    account_num: Mapped[str] = mapped_column(String(50), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
