"""process_progress: banked ticks of interrupted work (resume)

Revision ID: b4d7e1f9a3c6
Revises: a2c4e6f8b0d3
Create Date: 2026-10-27

The run-69 one-rung trap, engine side. A study that dies for want of a
per-tick input (or is cancelled) forfeited everything — lump inputs and
every paid tick — so the one failed attempt was also the last attempt:
no house ever re-tried the ladder. Work already paid is now banked in
`process_progress` (one row per entity+recipe) and re-starting the same
recipe resumes it: no second lump draw, `completes_tick` set to the
remaining ticks, the row cleared at completion. The credit is time, not
goods — conservation is untouched (everything consumed stays consumed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d7e1f9a3c6"
down_revision: Union[str, Sequence[str], None] = "a2c4e6f8b0d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "process_progress",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_id", sa.String(36),
                  sa.ForeignKey("entities.id"), nullable=False, index=True),
        sa.Column("recipe_id", sa.String(36),
                  sa.ForeignKey("recipes.id"), nullable=False),
        sa.Column("ticks_paid", sa.Integer(), nullable=False),
        sa.Column("effort", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entity_id", "recipe_id",
                            name="uq_process_progress"),
    )


def downgrade() -> None:
    op.drop_table("process_progress")
