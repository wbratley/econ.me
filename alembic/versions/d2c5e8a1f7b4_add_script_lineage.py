"""add_script_lineage

Revision ID: d2c5e8a1f7b4
Revises: c3a1f5e8b2d4
Create Date: 2026-08-01 00:00:00.000000

Adds ``lineage_id`` to ``scripts`` — the stable identity of a *law* across
enacted versions, so the governed script lifecycle (``services.set_script``,
docs/actors.md step 4a-1) can retire an active version and activate a new
one while preserving the full legislative history.

``lineage_id`` is what queries and voters refer to; ``name`` stays unique
per row and is auto-versioned (``{lineage_id}#{n}``) by the service. "The
current law" is the one row with ``lineage_id=X AND is_active=True``.

Existing scripts are backfilled to singleton lineages (``lineage_id = id``)
so nothing already created stops working.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2c5e8a1f7b4'
down_revision: Union[str, Sequence[str], None] = 'c3a1f5e8b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('scripts') as batch_op:
        batch_op.add_column(sa.Column('lineage_id', sa.String(length=255), nullable=True))
        batch_op.create_index('ix_scripts_lineage_id', ['lineage_id'])
    # each existing script becomes its own singleton lineage
    op.execute("UPDATE scripts SET lineage_id = id WHERE lineage_id IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('scripts') as batch_op:
        batch_op.drop_index('ix_scripts_lineage_id')
        batch_op.drop_column('lineage_id')
