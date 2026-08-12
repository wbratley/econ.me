"""add_entity_lifespan

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-07 00:00:00.000000

Adds ``entities.lifespan`` -- the age (in ticks) at which an entity dies
of old age, the invariant mortality floor of Step 6d (``docs/actors.md``).
The end-of-tick incapacity pass deactivates the entity once
``age = tick - birth_tick`` reaches this and applies the estate rule,
firing ``entity_incapacitated`` with ``condition: "age"`` (indistinguishable
to the engine from a starvation death: same event, same estate, same
insurance trigger).

NULL means *immortal* (the default): nothing already built ever dies of
old age, and the feature is opt-in. It is per-entity data, not a votable
WorldSetting -- the roadmap's \"not votable per tick\" makes a votable
lifespan self-defeating (the governance stack could repeal mortality
itself, which is the layer-1 scripted behaviour layer 2 exists to escape).
Stamped once at spawn/creation; no engine setter, so it is immutable the
way ``birth_tick`` and ``parents`` are. The world adjusts the *regime* by
amending the governed spawn POLICY (or the WorldSetting it reads); the
*dynamic* face of mortality stays the shipped condition/incapacitates_at
pass (food, medicine, needs, decay).

Existing rows are left NULL rather than backfilled: nothing predating this
migration is mortal. ``ctx.query.lifespan()`` reads nil for them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(sa.Column('lifespan', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_column('lifespan')
