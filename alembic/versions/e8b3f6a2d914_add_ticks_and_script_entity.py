"""add_ticks_and_script_entity

Revision ID: e8b3f6a2d914
Revises: 2d86d87714c9
Create Date: 2026-07-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8b3f6a2d914'
down_revision: Union[str, Sequence[str], None] = '2d86d87714c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('ticks',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('events', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('number'),
    )
    with op.batch_alter_table('scripts') as batch_op:
        batch_op.add_column(sa.Column('entity_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('state', sa.JSON(), nullable=False, server_default='{}'))
        batch_op.create_foreign_key('fk_scripts_entity_id_entities', 'entities', ['entity_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('scripts') as batch_op:
        batch_op.drop_constraint('fk_scripts_entity_id_entities', type_='foreignkey')
        batch_op.drop_column('state')
        batch_op.drop_column('entity_id')
    op.drop_table('ticks')
