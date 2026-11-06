from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from clearing_hub.core.database import Base
from clearing_hub.core.signing import fingerprint


class SignatureMode(StrEnum):
    """서명 전환 모드 (SETTLEMENT_PLAN 12.2 7번, SPEC 2.6). 관리자만 바꾼다. 공개키를 등록해도 저절로 바뀌지 않는다."""

    HMAC = "hmac"  # 은행 → 허브 HMAC만, 허브 → 은행 HMAC, 응답 서명 요구 없음 (기본, 기존 은행)
    BOTH = "both"  # 은행 → 허브 HMAC·Ed25519 둘 다, 허브 → 은행은 아직 HMAC. 은행이 보내는 쪽부터 바꿔 보는 기간
    ED25519 = "ed25519"  # 양방향 Ed25519만, 은행 응답에 응답 서명 필수 (없거나 틀리면 "응답 없음")


class Bank(Base):
    __tablename__ = "banks"

    bank_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    base_url: Mapped[str] = mapped_column(String(300), nullable=False)  # 예: https://b-bank.example.com/interbank
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 서명 비밀키. 허브가 은행을 부를 때 서명해야 하므로 해시로 둘 수 없다. 다음 단계: Fernet 암호화
    hmac_secret: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 순채무한도(원): 예치금보다 이만큼 더 보낼 수 있다. 담보·일중당좌대출 역할 (SETTLEMENT_PLAN 2절 6번)
    net_debit_cap: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # 부인방지 (SETTLEMENT_PLAN 12.3). 공개키만 받는다 (개인키는 은행만 갖는다). HMAC secret과는 따로 바뀐다
    signature_mode: Mapped[str] = mapped_column(String(10), default=SignatureMode.HMAC, server_default="hmac", nullable=False)
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
