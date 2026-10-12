"""S5b market escrows: goods physically at a placed market

Revision ID: w1f6a9c3e7b2
Revises: t5a8d2f6c1e4
Create Date: 2025-01-05
"""

from alembic import op
import sqlalchemy as sa


revision = "w1f6a9c3e7b2"
down_revision = "t5a8d2f6c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_escrows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("market_id", sa.String(36),
                  sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("seller_entity_id", sa.String(36),
                  sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4),
                  nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("market_id", "seller_entity_id", "symbol",
                            name="uq_market_escrow_pool"),
    )
    op.create_index(
        "ix_market_escrows_market", "market_escrows", ["market_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_market_escrows_market", table_name="market_escrows")
    op.drop_table("market_escrows")
