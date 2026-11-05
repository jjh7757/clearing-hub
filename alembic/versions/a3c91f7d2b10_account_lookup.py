"""본인 명의 계좌 조회 (SPEC 9절): account_lookups, account_refs

Revision ID: a3c91f7d2b10
Revises: ef6bf3b35409
Create Date: 2026-11-05 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c91f7d2b10'
down_revision: Union[str, Sequence[str], None] = 'ef6bf3b35409'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('account_lookups',
    sa.Column('lookup_id', sa.String(length=36), nullable=False),
    sa.Column('from_bank', sa.String(length=3), nullable=False),
    sa.Column('consent_id', sa.String(length=64), nullable=False),
    sa.Column('ci_hash', sa.String(length=64), nullable=True),
    sa.Column('results', sa.Text(), nullable=False),
    sa.Column('account_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['from_bank'], ['banks.bank_code'], ),
    sa.PrimaryKeyConstraint('lookup_id')
    )
    op.create_index(op.f('ix_account_lookups_from_bank'), 'account_lookups', ['from_bank'], unique=False)
    op.create_index(op.f('ix_account_lookups_ci_hash'), 'account_lookups', ['ci_hash'], unique=False)
    op.create_index(op.f('ix_account_lookups_created_at'), 'account_lookups', ['created_at'], unique=False)
    op.create_table('account_refs',
    sa.Column('ref', sa.String(length=64), nullable=False),
    sa.Column('lookup_id', sa.String(length=36), nullable=False),
    sa.Column('owner_bank', sa.String(length=3), nullable=False),
    sa.Column('bank_code', sa.String(length=3), nullable=False),
    sa.Column('account_num', sa.String(length=50), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bank_code'], ['banks.bank_code'], ),
    sa.ForeignKeyConstraint(['lookup_id'], ['account_lookups.lookup_id'], ),
    sa.ForeignKeyConstraint(['owner_bank'], ['banks.bank_code'], ),
    sa.PrimaryKeyConstraint('ref')
    )
    op.create_index(op.f('ix_account_refs_lookup_id'), 'account_refs', ['lookup_id'], unique=False)
    op.create_index(op.f('ix_account_refs_expires_at'), 'account_refs', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_account_refs_expires_at'), table_name='account_refs')
    op.drop_index(op.f('ix_account_refs_lookup_id'), table_name='account_refs')
    op.drop_table('account_refs')
    op.drop_index(op.f('ix_account_lookups_created_at'), table_name='account_lookups')
    op.drop_index(op.f('ix_account_lookups_ci_hash'), table_name='account_lookups')
    op.drop_index(op.f('ix_account_lookups_from_bank'), table_name='account_lookups')
    op.drop_table('account_lookups')
