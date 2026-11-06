from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from clearing_hub.core.database import Base
from clearing_hub.core.signing import fingerprint


class Bank(Base):
    __tablename__ = "banks"

    bank_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    base_url: Mapped[str] = mapped_column(String(300), nullable=False)  # 예: https://b-bank.example.com/interbank
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # X-Api-Key는 식별용. 서명은 공개키로 검증한다
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 순채무한도(원): 예치금보다 이만큼 더 보낼 수 있다. 담보·일중당좌대출 역할 (SETTLEMENT_PLAN 2절 6번)
    net_debit_cap: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # 부인방지 (SETTLEMENT_PLAN 12.3·12.11). 공개키만 받는다 (개인키는 은행만 갖는다). 허브는 은행의 비밀을 하나도 갖지 않는다.
    # 새 은행은 등록 때 반드시 넣는다. NULL은 8c 전에 등록된 은행뿐이며, 그 은행은 등록하기 전까지 401·PUBLIC_KEY_MISSING
    public_key: Mapped[str | None] = mapped_column(String(44))  # Ed25519 raw 32바이트 base64
    previous_public_key: Mapped[str | None] = mapped_column(String(44))  # 교체 전 키. previous_key_expires_at까지 받는다
    previous_key_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    public_key_registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def public_key_fingerprint(self) -> str | None:
        return fingerprint(self.public_key) if self.public_key else None

    def verification_keys(self, now: datetime | None = None) -> list[str]:
        """Ed25519 서명을 검증할 공개키: 현재 키, 그리고 만료 전이면 이전 키 (12.2 8번)."""
        keys = [self.public_key] if self.public_key else []
        expires = self.previous_key_expires_at
        if self.previous_public_key and expires is not None:
            expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)  # SQLite는 시간대 없이 준다
            if expires > (now or datetime.now(UTC)):
                keys.append(self.previous_public_key)
        return keys


class Invite(Base):
    """셀프 등록 초대 코드 (SETTLEMENT_PLAN 12.11, SPEC 3.5). 관리자가 팀마다 하나 발급하고, 은행이 한 번 쓴다."""

    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)  # inv_ + token_urlsafe(16)
    note: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")  # 팀 이름 등 관리자 메모
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_bank_code: Mapped[str | None] = mapped_column(String(3), ForeignKey("banks.bank_code"))
