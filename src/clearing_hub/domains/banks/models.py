from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from clearing_hub.core.database import Base


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
