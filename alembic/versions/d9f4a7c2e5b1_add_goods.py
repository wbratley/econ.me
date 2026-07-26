"""add_goods

Revision ID: d9f4a7c2e5b1
Revises: b7e2d5c8a916
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f4a7c2e5b1'
down_revision: Union[str, Sequence[str], None] = 'b7e2d5c8a916'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('goods',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=12), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('decay_per_tick', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('auto_issue_quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('auto_issue_entity_type', sa.Enum('INDIVIDUAL', 'BUSINESS', 'BANK', 'GOVERNMENT', name='entitytype'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('symbol'),
    )


def downgrade() -> None:
    op.drop_table('goods')
