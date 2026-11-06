import asyncio
import selectors
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from clearing_hub.core.config import get_settings
from clearing_hub.core.database import Base
from clearing_hub.domains.accounts.models import AccountLookup, AccountRef  # noqa: F401
from clearing_hub.domains.banks.models import Bank  # noqa: F401
from clearing_hub.domains.reserve.models import ReserveAccount, ReserveLedgerEntry  # noqa: F401
from clearing_hub.domains.settlement.models import SettlementPair, SettlementPosition, SettlementRun  # noqa: F401
from clearing_hub.domains.transfers.models import Transfer, TransferEvent  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif sys.platform == "win32":  # psycopg 비동기는 Windows 기본 Proactor 루프와 맞지 않는다
    asyncio.run(run_async_migrations(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
else:
    asyncio.run(run_async_migrations())
