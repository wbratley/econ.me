"""add monetary authority

Revision ID: c5d8e2f1a047
Revises: a3f9d2c1e8b4
Create Date: 2026-05-23 00:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d8e2f1a047'
down_revision: Union[str, Sequence[str], None] = 'a3f9d2c1e8b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(
            sa.Column('is_monetary_authority', sa.Boolean(), nullable=False, server_default='0')
        )


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_column('is_monetary_authority')
