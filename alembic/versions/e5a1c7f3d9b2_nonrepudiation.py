"""부인방지 (SETTLEMENT_PLAN 12.3, 7절 8단계 8a): banks 서명 모드·공개키, transfer_evidence

- banks.signature_mode (hmac | both | ed25519, 기본 hmac): 기존 은행은 hmac으로 시작해 아무것도 바꾸지 않아도 된다
- banks.public_key·previous_public_key (Ed25519 raw 32바이트 base64, 44자), previous_key_expires_at, public_key_registered_at
- transfer_evidence: 이체 접수 요청·/receive 요청·응답 원문 (추가만, 고치거나 지우지 않는다)

Revision ID: e5a1c7f3d9b2
Revises: d8f2b3a6e1c9
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5a1c7f3d9b2'
down_revision: Union[str, Sequence[str], None] = 'd8f2b3a6e1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('banks', sa.Column('signature_mode', sa.String(length=10), server_default='hmac', nullable=False))
    op.add_column('banks', sa.Column('public_key', sa.String(length=44), nullable=True))
    op.add_column('banks', sa.Column('previous_public_key', sa.String(length=44), nullable=True))
    op.add_column('banks', sa.Column('previous_key_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('banks', sa.Column('public_key_registered_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('transfer_evidence',
    sa.Column('evidence_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('transfer_id', sa.String(length=36), nullable=False),
    sa.Column('direction', sa.String(length=20), nullable=False),
    sa.Column('method', sa.String(length=10), nullable=True),
    sa.Column('path', sa.String(length=200), nullable=True),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('headers_json', sa.JSON(), nullable=False),
    sa.Column('body', sa.LargeBinary(), nullable=False),
    sa.Column('signature_alg', sa.String(length=10), nullable=True),
    sa.Column('signature_valid', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['transfer_id'], ['transfers.transfer_id'], ),
    sa.PrimaryKeyConstraint('evidence_id')
    )
    op.create_index(op.f('ix_transfer_evidence_transfer_id'), 'transfer_evidence', ['transfer_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_transfer_evidence_transfer_id'), table_name='transfer_evidence')
    op.drop_table('transfer_evidence')
    op.drop_column('banks', 'public_key_registered_at')
    op.drop_column('banks', 'previous_key_expires_at')
    op.drop_column('banks', 'previous_public_key')
    op.drop_column('banks', 'public_key')
    op.drop_column('banks', 'signature_mode')
