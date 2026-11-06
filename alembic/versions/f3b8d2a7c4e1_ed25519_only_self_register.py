"""Ed25519 필수화·셀프 등록 (SETTLEMENT_PLAN 12.11, 7절 8단계 8c): banks의 HMAC 칸 삭제, invites

- banks.hmac_secret 삭제: 허브는 은행의 비밀을 하나도 갖지 않는다 (공개키·API 키 해시만)
- banks.signature_mode 삭제: 서명 방식은 Ed25519 하나다
- invites: 셀프 등록 초대 코드 (code, note, created_at, expires_at, used_at, used_by_bank_code)

이미 있는 은행 중 public_key가 NULL인 은행(운영 901·902)은 막지 않는다. 그 은행은 공개키를 등록하기 전까지
요청이 401 SIGNATURE_INVALID, 받는 은행으로는 400 PUBLIC_KEY_MISSING이다. 그래서 **배포 전에** 관리자가
POST /admin/banks/{code}/public-key로 먼저 등록한다 (docs/DEPLOY.md "8c 배포 순서").

downgrade는 칸을 되살리지만 secret 값은 되돌릴 수 없다 (빈 값). 공개키가 있는 은행은 signature_mode를 ed25519로,
없는 은행은 hmac으로 두며, hmac 은행은 내린 뒤 관리자가 키를 다시 발급해야 한다.

Revision ID: f3b8d2a7c4e1
Revises: e5a1c7f3d9b2
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3b8d2a7c4e1'
down_revision: Union[str, Sequence[str], None] = 'e5a1c7f3d9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('invites',
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('note', sa.String(length=100), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('used_by_bank_code', sa.String(length=3), nullable=True),
    sa.ForeignKeyConstraint(['used_by_bank_code'], ['banks.bank_code'], ),
    sa.PrimaryKeyConstraint('code')
    )
    with op.batch_alter_table('banks') as batch:
        batch.drop_column('hmac_secret')
        batch.drop_column('signature_mode')


def downgrade() -> None:
    with op.batch_alter_table('banks') as batch:
        batch.add_column(sa.Column('signature_mode', sa.String(length=10), server_default='hmac', nullable=False))
        batch.add_column(sa.Column('hmac_secret', sa.String(length=100), server_default='', nullable=False))
    op.execute("UPDATE banks SET signature_mode = 'ed25519' WHERE public_key IS NOT NULL")
    op.drop_table('invites')
