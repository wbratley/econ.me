"""add_needs

Revision ID: e6a1d3b9c7f2
Revises: d9f4a7c2e5b1
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6a1d3b9c7f2'
down_revision: Union[str, Sequence[str], None] = 'd9f4a7c2e5b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('needs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('entity_type', sa.Enum('INDIVIDUAL', 'BUSINESS', 'BANK', 'GOVERNMENT', name='entitytype'), nullable=True),
    sa.Column('quantity_per_tick', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code'),
    )
    op.create_table('need_satisfiers',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('need_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=12), nullable=False),
    sa.ForeignKeyConstraint(['need_id'], ['needs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('need_id', 'symbol'),
    )
    op.create_table('need_states',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=False),
    sa.Column('need_id', sa.String(length=36), nullable=False),
    sa.Column('satisfaction', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('updated_tick', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ),
    sa.ForeignKeyConstraint(['need_id'], ['needs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entity_id', 'need_id'),
    )


def downgrade() -> None:
    op.drop_table('need_states')
    op.drop_table('need_satisfiers')
    op.drop_table('needs')
