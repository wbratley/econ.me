"""add_conditions

Revision ID: d4b8f1e6a923
Revises: b9e4c7a2f513
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4b8f1e6a923'
down_revision: Union[str, Sequence[str], None] = 'b9e4c7a2f513'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('world_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key'),
    )
    op.add_column('goods', sa.Column('modifies_pattern', sa.String(length=64), nullable=True))
    op.add_column('goods', sa.Column('modifies_factor', sa.Numeric(precision=5, scale=4), nullable=True))
    op.add_column('goods', sa.Column('incapacitates_at', sa.Numeric(precision=18, scale=4), nullable=True))
    op.add_column('needs', sa.Column('condition_symbol', sa.String(length=32), nullable=True))
    op.add_column('needs', sa.Column(
        'condition_quantity', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0'
    ))
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(sa.Column(
            'status', sa.Enum('ACTIVE', 'INCAPACITATED', name='entitystatus'),
            nullable=False, server_default='ACTIVE',
        ))
        batch_op.add_column(sa.Column('incapacitated_tick', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('heir_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key('fk_entities_heir_id_entities', 'entities', ['heir_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_constraint('fk_entities_heir_id_entities', type_='foreignkey')
        batch_op.drop_column('heir_id')
        batch_op.drop_column('incapacitated_tick')
        batch_op.drop_column('status')
    op.drop_column('needs', 'condition_quantity')
    op.drop_column('needs', 'condition_symbol')
    op.drop_column('goods', 'incapacitates_at')
    op.drop_column('goods', 'modifies_factor')
    op.drop_column('goods', 'modifies_pattern')
    op.drop_table('world_settings')
