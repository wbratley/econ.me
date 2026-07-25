"""add_commodity_markets

Revision ID: f4c7a9e1b3d8
Revises: e8b3f6a2d914
Create Date: 2026-07-12 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c7a9e1b3d8'
down_revision: Union[str, Sequence[str], None] = 'e8b3f6a2d914'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('markets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=12), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('last_price', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('symbol'),
    )
    op.create_table('holdings',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=12), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entity_id', 'symbol', name='uq_holdings_entity_symbol'),
    )
    op.create_table('orders',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('market_id', sa.String(length=36), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('side', sa.Enum('BUY', 'SELL', name='orderside'), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('remaining', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('limit_price', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('status', sa.Enum('OPEN', 'FILLED', 'CANCELLED', name='orderstatus'), nullable=False),
    sa.Column('reference', sa.String(length=500), nullable=False),
    sa.Column('cancel_reason', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], ),
    sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], ),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_orders_market_status', 'orders', ['market_id', 'status'])
    op.create_table('trades',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('market_id', sa.String(length=36), nullable=False),
    sa.Column('tick_number', sa.Integer(), nullable=False),
    sa.Column('buy_order_id', sa.String(length=36), nullable=False),
    sa.Column('sell_order_id', sa.String(length=36), nullable=False),
    sa.Column('buyer_entity_id', sa.String(length=36), nullable=False),
    sa.Column('seller_entity_id', sa.String(length=36), nullable=False),
    sa.Column('price', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], ),
    sa.ForeignKeyConstraint(['buy_order_id'], ['orders.id'], ),
    sa.ForeignKeyConstraint(['sell_order_id'], ['orders.id'], ),
    sa.ForeignKeyConstraint(['buyer_entity_id'], ['entities.id'], ),
    sa.ForeignKeyConstraint(['seller_entity_id'], ['entities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_trades_market', 'trades', ['market_id'])


def downgrade() -> None:
    op.drop_index('ix_trades_market', table_name='trades')
    op.drop_table('trades')
    op.drop_index('ix_orders_market_status', table_name='orders')
    op.drop_table('orders')
    op.drop_table('holdings')
    op.drop_table('markets')
