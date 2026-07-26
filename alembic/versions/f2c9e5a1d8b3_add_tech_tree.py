"""add_tech_tree

Adds the tech tree (technologies, prerequisites, unlocks, recipe gating)
and widens every symbol column from String(12) to String(32) to make room
for skilled-labor symbols like LABOR-SMITH / SKILL-SMITH.

Revision ID: f2c9e5a1d8b3
Revises: e6a1d3b9c7f2
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c9e5a1d8b3'
down_revision: Union[str, Sequence[str], None] = 'e6a1d3b9c7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column) pairs holding good symbols
_SYMBOL_COLUMNS = [
    ('markets', 'symbol'),
    ('goods', 'symbol'),
    ('holdings', 'symbol'),
    ('need_satisfiers', 'symbol'),
    ('recipe_inputs', 'symbol'),
    ('recipe_outputs', 'symbol'),
]


def upgrade() -> None:
    op.create_table('technologies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('scope', sa.Enum('ENTITY', 'WORLD', name='techscope'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code'),
    )
    op.create_table('technology_prerequisites',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('technology_id', sa.String(length=36), nullable=False),
    sa.Column('prerequisite_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['technology_id'], ['technologies.id'], ),
    sa.ForeignKeyConstraint(['prerequisite_id'], ['technologies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('technology_id', 'prerequisite_id'),
    )
    op.create_table('unlocks',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('technology_id', sa.String(length=36), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=True),
    sa.Column('unlocked_tick', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['technology_id'], ['technologies.id'], ),
    sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('technology_id', 'entity_id'),
    )
    op.create_table('recipe_requirements',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('technology_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.ForeignKeyConstraint(['technology_id'], ['technologies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'technology_id'),
    )
    op.create_table('recipe_unlocks',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('technology_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.ForeignKeyConstraint(['technology_id'], ['technologies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'technology_id'),
    )

    # batch mode: SQLite cannot ALTER COLUMN in place
    for table, column in _SYMBOL_COLUMNS:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=12),
                type_=sa.String(length=32),
                existing_nullable=False,
            )


def downgrade() -> None:
    for table, column in reversed(_SYMBOL_COLUMNS):
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=32),
                type_=sa.String(length=12),
                existing_nullable=False,
            )
    op.drop_table('recipe_unlocks')
    op.drop_table('recipe_requirements')
    op.drop_table('unlocks')
    op.drop_table('technology_prerequisites')
    op.drop_table('technologies')
