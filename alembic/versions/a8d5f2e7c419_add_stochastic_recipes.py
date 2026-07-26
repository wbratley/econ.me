"""add_stochastic_recipes

Adds outcome branch tables (recipe_branches, recipe_branch_outputs), the
per-process roll audit trail (processes.outcome_branch / outcome_roll), and
the tick event-list commitment (ticks.events_hash) that seeds the rolls.

Revision ID: a8d5f2e7c419
Revises: f2c9e5a1d8b3
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8d5f2e7c419'
down_revision: Union[str, Sequence[str], None] = 'f2c9e5a1d8b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('recipe_branches',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('weight', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('label', sa.String(length=255), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'position'),
    )
    op.create_table('recipe_branch_outputs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('branch_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['branch_id'], ['recipe_branches.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('processes', sa.Column('outcome_branch', sa.Integer(), nullable=True))
    op.add_column('processes', sa.Column('outcome_roll', sa.String(length=64), nullable=True))
    op.add_column('ticks', sa.Column('events_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('ticks', 'events_hash')
    op.drop_column('processes', 'outcome_roll')
    op.drop_column('processes', 'outcome_branch')
    op.drop_table('recipe_branch_outputs')
    op.drop_table('recipe_branches')
