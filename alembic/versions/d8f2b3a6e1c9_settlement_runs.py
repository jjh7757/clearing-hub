"""정산 배치 (SETTLEMENT_PLAN 4.1·4.2, 7절 3단계): settlement_runs·positions·pairs, transfers.settlement_run_id

- settlement_runs.settle_date는 유니크가 아니라 인덱스다 (한 날짜에 여러 run, 시연용 즉시 마감).
  대신 DRAFT는 동시에 하나만: 부분 유니크 인덱스 uq_settlement_runs_single_draft (status = 'DRAFT')
- transfers.settlement_run_id (FK nullable) + 인덱스 (from_bank, settlement_run_id), (to_bank, settlement_run_id)
- reserve_ledger.run_id에 FK settlement_runs.run_id (1단계에서 미뤄 둔 것)

FK 추가(op.create_foreign_key)는 ALTER TABLE ... ADD CONSTRAINT라 SQLite에서는 안 된다. 운영은 PostgreSQL이고
테스트는 마이그레이션이 아니라 create_all을 쓰므로 batch 모드는 쓰지 않는다.
FK 이름은 PostgreSQL 기본 이름(<테이블>_<컬럼>_fkey)과 같게 둔다: 모델은 이름 없는 ForeignKey라 create_all과 맞춘다.

Revision ID: d8f2b3a6e1c9
Revises: c7e1a9d4f2b6
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd8f2b3a6e1c9'
down_revision: Union[str, Sequence[str], None] = 'c7e1a9d4f2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('settlement_runs',
    sa.Column('run_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('settle_date', sa.Date(), nullable=False),
    sa.Column('cutoff_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('tx_count', sa.Integer(), nullable=False),
    sa.Column('total_amount', sa.BigInteger(), nullable=False),
    sa.Column('created_by', sa.String(length=50), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('settled_by', sa.String(length=50), nullable=True),
    sa.PrimaryKeyConstraint('run_id')
    )
    op.create_index(op.f('ix_settlement_runs_settle_date'), 'settlement_runs', ['settle_date'], unique=False)
    op.create_index('uq_settlement_runs_single_draft', 'settlement_runs', ['status'], unique=True,
                    postgresql_where=sa.text("status = 'DRAFT'"), sqlite_where=sa.text("status = 'DRAFT'"))
    op.create_table('settlement_positions',
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('bank_code', sa.String(length=3), nullable=False),
    sa.Column('sent_count', sa.Integer(), nullable=False),
    sa.Column('sent_total', sa.BigInteger(), nullable=False),
    sa.Column('recv_count', sa.Integer(), nullable=False),
    sa.Column('recv_total', sa.BigInteger(), nullable=False),
    sa.Column('net_amount', sa.BigInteger(), nullable=False),
    sa.Column('result', sa.String(length=20), nullable=True),
    sa.Column('balance_after', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['bank_code'], ['banks.bank_code'], ),
    sa.ForeignKeyConstraint(['run_id'], ['settlement_runs.run_id'], ),
    sa.PrimaryKeyConstraint('run_id', 'bank_code')
    )
    op.create_table('settlement_pairs',
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('bank_a', sa.String(length=3), nullable=False),
    sa.Column('bank_b', sa.String(length=3), nullable=False),
    sa.Column('a_to_b_count', sa.Integer(), nullable=False),
    sa.Column('a_to_b_total', sa.BigInteger(), nullable=False),
    sa.Column('b_to_a_count', sa.Integer(), nullable=False),
    sa.Column('b_to_a_total', sa.BigInteger(), nullable=False),
    sa.Column('net_amount', sa.BigInteger(), nullable=False),
    sa.Column('net_direction', sa.String(length=10), nullable=False),
    sa.CheckConstraint('bank_a < bank_b', name='ck_settlement_pairs_order'),
    sa.ForeignKeyConstraint(['bank_a'], ['banks.bank_code'], ),
    sa.ForeignKeyConstraint(['bank_b'], ['banks.bank_code'], ),
    sa.ForeignKeyConstraint(['run_id'], ['settlement_runs.run_id'], ),
    sa.PrimaryKeyConstraint('run_id', 'bank_a', 'bank_b')
    )
    op.add_column('transfers', sa.Column('settlement_run_id', sa.Integer(), nullable=True))
    op.create_foreign_key('transfers_settlement_run_id_fkey', 'transfers', 'settlement_runs',
                          ['settlement_run_id'], ['run_id'])
    op.create_index('ix_transfers_from_bank_settlement', 'transfers', ['from_bank', 'settlement_run_id'], unique=False)
    op.create_index('ix_transfers_to_bank_settlement', 'transfers', ['to_bank', 'settlement_run_id'], unique=False)
    op.create_foreign_key('reserve_ledger_run_id_fkey', 'reserve_ledger', 'settlement_runs', ['run_id'], ['run_id'])


def downgrade() -> None:
    op.drop_constraint('reserve_ledger_run_id_fkey', 'reserve_ledger', type_='foreignkey')
    op.drop_index('ix_transfers_to_bank_settlement', table_name='transfers')
    op.drop_index('ix_transfers_from_bank_settlement', table_name='transfers')
    op.drop_constraint('transfers_settlement_run_id_fkey', 'transfers', type_='foreignkey')
    op.drop_column('transfers', 'settlement_run_id')
    op.drop_table('settlement_pairs')
    op.drop_table('settlement_positions')
    op.drop_index('uq_settlement_runs_single_draft', table_name='settlement_runs',
                  postgresql_where=sa.text("status = 'DRAFT'"), sqlite_where=sa.text("status = 'DRAFT'"))
    op.drop_index(op.f('ix_settlement_runs_settle_date'), table_name='settlement_runs')
    op.drop_table('settlement_runs')
