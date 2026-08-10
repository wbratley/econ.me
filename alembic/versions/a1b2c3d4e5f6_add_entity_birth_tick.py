"""add_entity_birth_tick

Revision ID: a1b2c3d4e5f6
Revises: f8d2a1c4b6e9
Create Date: 2026-08-05 00:00:00.000000

Adds ``entities.birth_tick`` -- the tick an entity came into being. Age is
the one entity attribute that is NOT a holding (it is monotonic and
tick-derived: ``age = ctx.tick - birth_tick``), so it is a derived value
rather than a grantable/decayable good. Step 6a, ``docs/actors.md``.

Set once at creation (``services.create_entity`` stamps the latest
committed tick), never mutated -- age is unforgeable the way holdings are.
``ctx.query.age(entity_id)`` reads nil for a NULL birth_tick (an entity
that predates age-tracking), so an age-gating script written fail-closed
treats the unknown as "eligibility cannot be certified".

Existing rows are left NULL rather than backfilled: a long-running world's
old entities would read a wrong age (a 500-tick-old entity would appear 0)
under any backfill, and no script reads ``age()`` before this migration, so
existing runs are unaffected either way. New entities created after the
migration are tracked from birth.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f8d2a1c4b6e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(sa.Column('birth_tick', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_column('birth_tick')
