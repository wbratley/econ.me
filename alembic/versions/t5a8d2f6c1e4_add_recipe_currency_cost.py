"""add recipe currency_cost

Run 49's postmortem: the trading post coin-broke at t224 -- it bought
goods (14 eggs, ~60 coin) with no resale channel, and the market died
with a corpse counter for 60% of the run. The merchant rework gives
recipes a COIN leg: currency_cost is debited from the crafter's account
at start (the wholesale channel PAYS for stock, coin first). The credit
side needed no column -- a banked output symbol already mints to the
account.

Revision ID: t5a8d2f6c1e4
Revises: s6d9e3c8b2f4
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "t5a8d2f6c1e4"
down_revision = "s6d9e3c8b2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recipes",
        sa.Column("currency_cost", sa.Numeric(precision=18, scale=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recipes", "currency_cost")
