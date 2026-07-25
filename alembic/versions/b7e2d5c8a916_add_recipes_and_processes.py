"""add_recipes_and_processes

Revision ID: b7e2d5c8a916
Revises: f4c7a9e1b3d8
Create Date: 2026-07-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2d5c8a916'
down_revision: Union[str, Sequence[str], None] = 'f4c7a9e1b3d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('recipes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('duration_ticks', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code'),
    )
    op.create_table('recipe_inputs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=12), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('recipe_outputs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=12), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('processes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=False),
    sa.Column('started_tick', sa.Integer(), nullable=False),
    sa.Column('completes_tick', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('RUNNING', 'COMPLETED', 'CANCELLED', name='processstatus'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_processes_status_completes', 'processes', ['status', 'completes_tick'])


def downgrade() -> None:
    op.drop_index('ix_processes_status_completes', table_name='processes')
    op.drop_table('processes')
    op.drop_table('recipe_outputs')
    op.drop_table('recipe_inputs')
    op.drop_table('recipes')
