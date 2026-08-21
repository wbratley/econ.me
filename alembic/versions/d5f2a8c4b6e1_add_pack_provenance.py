"""pack provenance on content rows

Revision ID: d5f2a8c4b6e1
Revises: b3e8d1f6a9c4
Create Date: 2026-08-21 20:10:00.000000

Phase 3d (game.md §15.4): the content-pack standard envelope. Content
rows — Good, Recipe, Technology, Need, Market — gain a nullable
`pack_id`: the id of the pack that installed the row, so the catalog
attributes every row and a world knows what it is running. NULL =
platform/legacy content. The install-time conflict rule lives in the
create helpers: a second pack claiming an existing unique key is a
clean ValueError, not an IntegrityError from the constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5f2a8c4b6e1'
down_revision: Union[str, Sequence[str], None] = 'b3e8d1f6a9c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('goods', 'recipes', 'technologies', 'needs', 'markets')


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column('pack_id', sa.String(length=32),
                                          nullable=True))


def downgrade() -> None:
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column('pack_id')
