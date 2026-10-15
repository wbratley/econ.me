"""add the season calendar (run 54 E1)

Revision ID: u7c3f5a9e2d6
Revises: t5a8d2f6c1e4
Create Date: 2026-10-10

Three nullable columns carry what the packs declare; the calendar
itself (SEASONS, the 6-day default, the votable clock.season_days
WorldSetting) is engine code in clock.py, not schema:

- recipes.requires_season -- JSON list of season names the recipe runs
  in (NULL = every season, all of history)
- needs.season_quantity_per_tick -- JSON {SEASON: "1.0000"} day-draw
  overrides (NULL = the draw never varies by season)
- needs.season_night_quantity_per_tick -- the night-side twin
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'u7c3f5a9e2d6'
down_revision: Union[str, Sequence[str], None] = 't5a8d2f6c1e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'recipes',
        sa.Column('requires_season', sa.JSON(), nullable=True),
    )
    op.add_column(
        'needs',
        sa.Column('season_quantity_per_tick', sa.JSON(), nullable=True),
    )
    op.add_column(
        'needs',
        sa.Column('season_night_quantity_per_tick', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('needs', 'season_night_quantity_per_tick')
    op.drop_column('needs', 'season_quantity_per_tick')
    op.drop_column('recipes', 'requires_season')
