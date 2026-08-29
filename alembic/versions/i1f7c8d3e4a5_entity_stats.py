"""add entity_stats table

Run ID: stone run 20 -- wolves as entities. The predator stops being
an abstract pressure and becomes a creature: entities carry combat
stats (ATTACK, DEFENSE, ...) as authored rows, health is a HITS
holding every creature spawns with, and combat resolves between two
entities under pack-declared rules (COMBAT_RULES world setting:
weapons, armor, deterrence, loot). Kills ride the existing
incapacity/estate machinery; combat events are loud facts the witness
carries to every rival. Population renews via the spawns pass
(SPAWN_RULES setting) at round boundaries.

Revision ID: i1f7c8d3e4a5
Revises: h9e5f2b7c8d2
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'i1f7c8d3e4a5'
down_revision: Union[str, Sequence[str], None] = 'h9e5f2b7c8d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'entity_stats',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('entity_id', sa.String(36),
                  sa.ForeignKey('entities.id'), nullable=False),
        sa.Column('stat', sa.String(32), nullable=False),
        sa.Column('value', sa.Numeric(precision=18, scale=4),
                  nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('entity_id', 'stat'),
    )


def downgrade() -> None:
    op.drop_table('entity_stats')
