"""P5 the larder: recipes.scales_with and processes.scale_factor

Revision ID: q3f8b1c5d2e7
Revises: o2d5c8a7f3b1
Create Date: 2026-09-13

Pastoral capital (production.py): a recipe may scale its outputs by the
units of a held, never-consumed catalyst good ({SYMBOL: cap} on the
recipe) -- the harvest is priced by the herd. The applied factor rides
the process row so the completion event records a fact, not a
recomputation.
"""
from alembic import op
import sqlalchemy as sa

revision: str = 'q3f8b1c5d2e7'
down_revision = 'o2d5c8a7f3b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'recipes',
        sa.Column('scales_with', sa.JSON(), nullable=True),
    )
    op.add_column(
        'processes',
        sa.Column('scale_factor', sa.Numeric(precision=18, scale=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('processes', 'scale_factor')
    op.drop_column('recipes', 'scales_with')
