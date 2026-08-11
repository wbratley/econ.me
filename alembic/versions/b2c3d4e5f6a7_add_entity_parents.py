"""add_entity_parents

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-06 00:00:00.000000

Adds ``entities.parents`` -- a generic JSON list of parent entity ids,
stamped once by ``spawn_entity`` and never mutated. Provenance is the one
datum of reproduction that MUST be engine-owned: lineage has to be
authoritative for inheritance (``heir_id``) and consanguinity rules
(\"these two are siblings\"), so it cannot live in scribbleable script
state, and it is immutable for the same reason ``birth_tick`` is. Step 6c,
``docs/actors.md``.

The engine STORES the list; it does NOT interpret it -- two-parent
biology, one-parent manufacturing, and zero-parent spontaneous generation
are just different-length lists. Sex/marriage/permit are deliberately not
columns: they are data (holdings / WorldSettings / capabilities) that
VALIDATOR scripts read to compose the world's birth rules.

Existing rows are left NULL rather than backfilled: entities made at world
setup predate spawn-tracking, and NULL cleanly means \"no recorded
parents\" (``ctx.query.parents()`` reads an empty table for them), the way
a NULL ``birth_tick`` means \"predates age-tracking\".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(sa.Column('parents', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_column('parents')
