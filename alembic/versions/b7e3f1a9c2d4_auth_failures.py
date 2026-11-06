"""10단계 (SETTLEMENT_PLAN 14절): 연동 진단 auth_failures

- auth_failures: 인증 실패 한 건. direction BANK_TO_HUB(허브가 401) | HUB_TO_BANK(은행이 401·403, 은행 응답 서명 틀림).
  claimed_bank_code(헤더 값), bank_code(식별된 은행, 키가 틀리면 NULL), reason, message, method, path, query, url,
  headers_json(서명 헤더·X-Request-ID만, X-Api-Key 없음), api_key_sent, body(64KB까지)·body_truncated,
  http_status·response_headers_json·response_body·response_signature_valid(HUB_TO_BANK), created_at.
  은행당 최근 200건만 남긴다 (기록 때 지운다). 로그 표라 FK는 없다

기존 테이블은 바꾸지 않는다.

Revision ID: b7e3f1a9c2d4
Revises: a9d4e2c6b8f1
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e3f1a9c2d4'
down_revision: Union[str, Sequence[str], None] = 'a9d4e2c6b8f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('auth_failures',
    sa.Column('failure_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('direction', sa.String(length=12), nullable=False),
    sa.Column('claimed_bank_code', sa.String(length=20), nullable=True),
    sa.Column('bank_code', sa.String(length=3), nullable=True),
    sa.Column('reason', sa.String(length=40), nullable=False),
    sa.Column('message', sa.String(length=300), nullable=True),
    sa.Column('method', sa.String(length=10), nullable=False),
    sa.Column('path', sa.String(length=300), nullable=False),
    sa.Column('query', sa.String(length=300), nullable=True),
    sa.Column('url', sa.String(length=500), nullable=True),
    sa.Column('headers_json', sa.JSON(), nullable=False),
    sa.Column('api_key_sent', sa.Boolean(), nullable=True),
    sa.Column('body', sa.LargeBinary(), nullable=False),
    sa.Column('body_truncated', sa.Boolean(), nullable=False),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('response_headers_json', sa.JSON(), nullable=True),
    sa.Column('response_body', sa.LargeBinary(), nullable=True),
    sa.Column('response_signature_valid', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('failure_id')
    )
    op.create_index('ix_auth_failures_bank', 'auth_failures', ['bank_code', 'failure_id'], unique=False)
    op.create_index('ix_auth_failures_claimed', 'auth_failures', ['claimed_bank_code', 'failure_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_auth_failures_claimed', table_name='auth_failures')
    op.drop_index('ix_auth_failures_bank', table_name='auth_failures')
    op.drop_table('auth_failures')
