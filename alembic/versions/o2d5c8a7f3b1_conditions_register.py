"""conditions register: entity light memory, route waiting, recipe condition gate

Revision ID: o2d5c8a7f3b1
Revises: n8f4a1c6e0d3
Create Date: 2026-09-27

The torch rework's engine side (ROADMAP P2). The register itself is
data (`conditions.rules` world setting) -- named, per-tick, queryable
conditions derived from state the engine already keeps. Three columns
give it memory and teeth: `entities.last_lit_tick` (the ember window's
memory: the last tick a light-holding condition was satisfied), 
`travel_routes.waiting_since_tick` (a night halt that may resume on a
relight or dawn, strands after `travel.rules`' window), and
`recipes.requires_conditions` (a start-gate over register condition
names -- how conditions activate or refuse actions). All defaults
reproduce yesterday's behavior.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'o2d5c8a7f3b1'
down_revision: Union[str, Sequence[str], None] = 'n8f4a1c6e0d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('entities', sa.Column('last_lit_tick', sa.Integer(), nullable=True))
    op.add_column('travel_routes', sa.Column('waiting_since_tick', sa.Integer(), nullable=True))
    op.add_column('recipes', sa.Column('requires_conditions', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('recipes', 'requires_conditions')
    op.drop_column('travel_routes', 'waiting_since_tick')
    op.drop_column('entities', 'last_lit_tick')
