"""add per-tick recipe inputs

Revision ID: e1f9a2c4b7d8
Revises: d4b8f1e6a923
Create Date: 2026-07-27 12:00:00.000000

A new recipe sub-table, recipe_per_tick_inputs, for inputs consumed once per
tick a process is RUNNING rather than once at start -- the recurring-cost
counterpart to recipe_inputs. Lets a multi-tick process (research,
construction) be paid out of a flow income instead of an unreachable lump
stock.

Note on the processstatus enum: production.consume_per_tick_inputs marks a
process FAILED when it cannot meet a per-tick input. On SQLite (this app's
only dialect) the column is VARCHAR(9) with no CHECK constraint, so 'failed'
(6 chars) is accepted with no DDL change -- the status is enforced at the
ORM layer. Should this project move to PostgreSQL, run
`ALTER TYPE processstatus ADD VALUE 'failed'` (outside a transaction).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f9a2c4b7d8'
down_revision: Union[str, Sequence[str], None] = 'd4b8f1e6a923'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'recipe_per_tick_inputs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('recipe_id', sa.String(length=36), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('recipe_id', 'symbol'),
    )


def downgrade() -> None:
    op.drop_table('recipe_per_tick_inputs')
