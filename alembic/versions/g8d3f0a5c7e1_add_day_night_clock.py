"""add day/night clock columns

Run ID: stone run 18 -- tick becomes the hour (round = 24 ticks = one
day). Three nullable-default columns carry what the packs declare:

- needs.night_quantity_per_tick  -- the draw during dark hours (NULL =
  the draw never varies)
- goods.auto_issue_daylight_only -- top-up only during daylight (the
  LABOR ration is a daylight wage: 14 labor-hours a day, zero at night)
- recipes.requires_daylight      -- refused at night with a darkness
  error (GATHER/HUNT need light)

Revision ID: g8d3f0a5c7e1
Revises: e7c4a9b2d8f1
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'g8d3f0a5c7e1'
down_revision: Union[str, Sequence[str], None] = 'e7c4a9b2d8f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'needs',
        sa.Column('night_quantity_per_tick',
                  sa.Numeric(precision=18, scale=4), nullable=True),
    )
    op.add_column(
        'goods',
        sa.Column('auto_issue_daylight_only', sa.Boolean(),
                  nullable=False, server_default=sa.text('false')),
    )
    op.add_column(
        'recipes',
        sa.Column('requires_daylight', sa.Boolean(),
                  nullable=False, server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('recipes', 'requires_daylight')
    op.drop_column('goods', 'auto_issue_daylight_only')
    op.drop_column('needs', 'night_quantity_per_tick')
