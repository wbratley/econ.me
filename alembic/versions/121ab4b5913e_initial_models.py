"""initial models

Revision ID: 121ab4b5913e
Revises:
Create Date: 2026-05-22 17:14:37.617948

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '121ab4b5913e'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'entities',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('entity_type', sa.Enum('individual', 'business', 'bank', 'government', name='entitytype'), nullable=False),
    )
    op.create_table(
        'accounts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('entity_id', sa.String(36), sa.ForeignKey('entities.id'), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('balance', sa.Numeric(precision=18, scale=4), nullable=False),
    )
    op.create_table(
        'transactions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('accounts.id'), nullable=False),
        sa.Column('date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('tx_type', sa.Enum('debit', 'credit', name='transactiontype'), nullable=False),
        sa.Column('from_account_id', sa.String(36), sa.ForeignKey('accounts.id'), nullable=True),
        sa.Column('to_account_id', sa.String(36), sa.ForeignKey('accounts.id'), nullable=True),
        sa.Column('reference', sa.String(500), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('transactions')
    op.drop_table('accounts')
    op.drop_table('entities')
