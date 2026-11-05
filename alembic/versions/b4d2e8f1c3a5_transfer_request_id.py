"""transfers.request_id: X-Request-ID 추적 (SPEC 2.4)

Revision ID: b4d2e8f1c3a5
Revises: a3c91f7d2b10
Create Date: 2026-11-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4d2e8f1c3a5'
down_revision: Union[str, Sequence[str], None] = 'a3c91f7d2b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('transfers', sa.Column('request_id', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('transfers', 'request_id')
