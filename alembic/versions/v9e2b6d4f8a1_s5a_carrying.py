"""s5a: goods have weight, the road has a limit

Run 54 E2. spatial.md's S5a lands with the departure gate instead of the
mid-journey manifest snapshot (the deviation is argued in the run-54
spec: a walker eating their own rations on the road is lunch, not an
exploit -- the doc's goal was capacity, and capacity is what ships).

- goods.carry_exempt: body goods (warmth, rest, water, labor,
  conditions, hits -- pack-declared data) never weigh against the road.
- recipes.carry_capacity: a travel template's road limit. NULL = the
  traveller's CARRY stat decides; no stat and no number = no gate, so
  legacy worlds run exactly as before.

Revision ID: v9e2b6d4f8a1
Revises: t5a8d2f6c1e4
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa

revision = "v9e2b6d4f8a1"
down_revision = "t5a8d2f6c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "goods",
        sa.Column("carry_exempt", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )
    op.add_column(
        "recipes",
        sa.Column("carry_capacity", sa.Numeric(precision=18, scale=4),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recipes", "carry_capacity")
    op.drop_column("goods", "carry_exempt")
