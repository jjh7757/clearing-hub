from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from clearing_hub.core.config import get_settings

settings = get_settings()
pool = {"pool_size": 5, "max_overflow": 5} if settings.database_url.startswith("postgresql") else {}
engine = create_async_engine(settings.database_url, echo=settings.db_echo, **pool)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as db:
        yield db
