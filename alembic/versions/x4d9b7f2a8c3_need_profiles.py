"""need profiles + holding-rate modifiers (run 56)

The run-55 lesson: needs are declared per entity TYPE, but creatures are
not types -- the wolf is an INDIVIDUAL that should not want comfort the
way a house does. Two columns:

- ``entities.need_profile`` -- nullable JSON, stamped from the spawn
  template at birth: {"omit": ["COMFORT"], "rates": {"FOOD": "0.5"}}.
  NULL = the default body (every need of the entity's type applies at
  its declared rate, the pre-run-56 world exactly).
- ``needs.holding_rate_modifiers`` -- nullable JSON, pack data on the
  need: {holding_symbol: per-point multiplier delta}; the draw scales
  by what the body holds, floored at zero.

Revision ID: x4d9b7f2a8c3
Revises: w1f6a9c3e7b2
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa

revision = "x4d9b7f2a8c3"
down_revision = "w1f6a9c3e7b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("entities") as batch:
        batch.add_column(sa.Column("need_profile", sa.JSON(), nullable=True))
    with op.batch_alter_table("needs") as batch:
        batch.add_column(sa.Column("holding_rate_modifiers", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("entities") as batch:
        batch.drop_column("need_profile")
    with op.batch_alter_table("needs") as batch:
        batch.drop_column("holding_rate_modifiers")
