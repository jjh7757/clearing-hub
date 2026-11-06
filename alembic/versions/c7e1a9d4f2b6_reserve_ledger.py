"""예치금 원장 (SETTLEMENT_PLAN 4.1·4.2, 7절 1단계): reserve_accounts, reserve_ledger, banks.net_debit_cap

기존 은행의 reserve_accounts 행은 만들지 않는다. reserve 서비스가 처음 쓸 때 잔액 0으로 만든다 (get-or-create).
reserve_ledger.run_id의 FK(settlement_runs)는 3단계 마이그레이션에서 붙인다.

Revision ID: c7e1a9d4f2b6
Revises: b4d2e8f1c3a5
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7e1a9d4f2b6'
down_revision: Union[str, Sequence[str], None] = 'b4d2e8f1c3a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('banks', sa.Column('net_debit_cap', sa.BigInteger(), server_default='0', nullable=False))
    op.create_table('reserve_accounts',
    sa.Column('bank_code', sa.String(length=3), nullable=False),
    sa.Column('balance', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bank_code'], ['banks.bank_code'], ),
    sa.PrimaryKeyConstraint('bank_code')
    )
    op.create_table('reserve_ledger',
    sa.Column('entry_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('bank_code', sa.String(length=3), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('balance_after', sa.BigInteger(), nullable=False),
    sa.Column('memo', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.String(length=50), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bank_code'], ['banks.bank_code'], ),
    sa.PrimaryKeyConstraint('entry_id'),
    sa.UniqueConstraint('run_id', 'bank_code', 'kind', name='uq_reserve_ledger_run')
    )
    op.create_index(op.f('ix_reserve_ledger_bank_code'), 'reserve_ledger', ['bank_code'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reserve_ledger_bank_code'), table_name='reserve_ledger')
    op.drop_table('reserve_ledger')
    op.drop_table('reserve_accounts')
    op.drop_column('banks', 'net_debit_cap')
