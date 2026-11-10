"""예치금 입금·인출 신청 reserve_requests (SPEC 10.9)

- reserve_requests: 은행이 신청하고 관리자가 승인·거절한다. (bank_code, request_ref) 유일.
  승인되면 entry_id에 남긴 예치금 분개(reserve_ledger)를 적는다

기존 테이블은 바꾸지 않는다.

Revision ID: c2f6a8d1e4b7
Revises: b7e3f1a9c2d4
Create Date: 2026-11-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2f6a8d1e4b7'
down_revision: Union[str, Sequence[str], None] = 'b7e3f1a9c2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('reserve_requests',
    sa.Column('request_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('bank_code', sa.String(length=3), nullable=False),
    sa.Column('request_ref', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('memo', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('reject_reason', sa.String(length=200), nullable=True),
    sa.Column('entry_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_by', sa.String(length=50), nullable=True),
    sa.ForeignKeyConstraint(['bank_code'], ['banks.bank_code'], ),
    sa.ForeignKeyConstraint(['entry_id'], ['reserve_ledger.entry_id'], ),
    sa.PrimaryKeyConstraint('request_id'),
    sa.UniqueConstraint('bank_code', 'request_ref', name='uq_reserve_request_ref')
    )
    op.create_index(op.f('ix_reserve_requests_bank_code'), 'reserve_requests', ['bank_code'], unique=False)
    op.create_index(op.f('ix_reserve_requests_status'), 'reserve_requests', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reserve_requests_status'), table_name='reserve_requests')
    op.drop_index(op.f('ix_reserve_requests_bank_code'), table_name='reserve_requests')
    op.drop_table('reserve_requests')
