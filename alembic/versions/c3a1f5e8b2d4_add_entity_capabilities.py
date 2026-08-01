"""add_entity_capabilities

Revision ID: c3a1f5e8b2d4
Revises: e1f9a2c4b7d8
Create Date: 2026-07-31 23:30:00.000000

Adds the `capabilities` column to `entities` — the privilege set that gates
non-self-directed action (tax, seizure, policy). See docs/actors.md Fork 2.
The legacy `is_monetary_authority` flag is untouched; it keeps working as a
backward-compatible alias for the monetary capability.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3a1f5e8b2d4'
down_revision: Union[str, Sequence[str], None] = 'e1f9a2c4b7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(sa.Column(
            'capabilities', sa.JSON(), nullable=False, server_default='[]'
        ))


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_column('capabilities')
