"""catalog descriptions on the readable-world rows

Revision ID: b3e8d1f6a9c4
Revises: c7d9e1f3a5b2
Create Date: 2026-08-21 18:40:00.000000

Phase 3a (game.md §15.1): every catalog row -- Good, Recipe, Technology,
Need, Market -- gains a `description` column (one or two sentences; `""`
keeps every existing world valid). `name` columns already ship unfilled;
this adds the authored half of the catalog. The derived half (condition
effect lines, decay, modifies, auto-issue, branch odds, gates) is
generated at read time by `econengine.catalog` -- prose that cannot
drift from physics because the prose is a function of the physics.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3e8d1f6a9c4'
down_revision: Union[str, Sequence[str], None] = 'c7d9e1f3a5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('goods', 'recipes', 'technologies', 'needs', 'markets')


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column('description', sa.String(length=1000),
                          nullable=False, server_default=''))


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column('description')
