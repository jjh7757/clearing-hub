"""정산 9단계 (SETTLEMENT_PLAN 13절): 은행의 대사 결과 settlement_acks

- settlement_acks: run마다 은행당 한 행 (PK run_id, bank_code). result ACK | DISPUTE, transfer_ids_json(dispute 때
  차이 나는 transfer_id 목록, JSON 문자열), message, created_at(처음), updated_at(마지막). 다시 보내면 덮어쓴다

기존 테이블은 바꾸지 않는다. 셀프 시험 거래는 새 칸 없이 보내는 은행(SIM_BANK_CODE)과 from_account로 가린다.

Revision ID: a9d4e2c6b8f1
Revises: f3b8d2a7c4e1
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9d4e2c6b8f1'
down_revision: Union[str, Sequence[str], None] = 'f3b8d2a7c4e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('settlement_acks',
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('bank_code', sa.String(length=3), nullable=False),
    sa.Column('result', sa.String(length=10), nullable=False),
    sa.Column('transfer_ids_json', sa.Text(), nullable=False),
    sa.Column('message', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bank_code'], ['banks.bank_code'], ),
    sa.ForeignKeyConstraint(['run_id'], ['settlement_runs.run_id'], ),
    sa.PrimaryKeyConstraint('run_id', 'bank_code')
    )


def downgrade() -> None:
    op.drop_table('settlement_acks')
