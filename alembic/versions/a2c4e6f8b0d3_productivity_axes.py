"""Productivity axes: condition factor, recipe axis, applied P

Option 2 of docs/productivity.md — conditions as productivity modifiers
on outcomes. Three tracking-only columns:

- goods.productivity_factor: while held, a condition good's work-
  conversion factor (NULL = no tax, reads 1; condition goods only).
- recipes.effect_kind: the axis a recipe applies P along ("yield" /
  "success" / NULL = "none").
- processes.productivity: the P actually applied at completion (NULL =
  no tax, reads 1), stamped next to scale_factor / effort_factor.

Revision ID: a2c4e6f8b0d3
Revises: z9a4c2e7f1d8
Create Date: 2025-01-06
"""

from alembic import op
import sqlalchemy as sa


revision = "a2c4e6f8b0d3"
down_revision = "z9a4c2e7f1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "goods",
        sa.Column(
            "productivity_factor",
            sa.Numeric(precision=5, scale=4),
            nullable=True,
        ),
    )
    op.add_column(
        "recipes",
        sa.Column(
            "effect_kind",
            sa.String(length=16),
            nullable=True,
        ),
    )
    op.add_column(
        "processes",
        sa.Column(
            "productivity",
            sa.Numeric(precision=18, scale=4),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("processes", "productivity")
    op.drop_column("recipes", "effect_kind")
    op.drop_column("goods", "productivity_factor")
