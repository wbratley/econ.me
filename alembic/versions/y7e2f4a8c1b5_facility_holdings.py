"""Facility holdings: goods physically at a facility's shelf

Revision ID: y7e2f4a8c1b5
Revises: x4d9b7f2a8c3
Create Date: 2025-01-05
"""

from alembic import op
import sqlalchemy as sa


revision = "y7e2f4a8c1b5"
down_revision = "x4d9b7f2a8c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "facility_holdings",
        sa.Column("id", sa.String(36), primary_key=True),
        # No FK on owner_id: it is an entity id (a private store row) or
        # the "*" shared-pool sentinel — the constraint is DB-enforced
        # only because the sentinel makes NULLs unnecessary (SQLite
        # treats NULLs as distinct in a UNIQUE index).
        sa.Column("facility_id", sa.String(36),
                  sa.ForeignKey("facilities.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4),
                  nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("facility_id", "owner_id", "symbol",
                            name="uq_facility_holding"),
    )
    op.create_index(
        "ix_facility_holdings_facility", "facility_holdings",
        ["facility_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_facility_holdings_facility",
                 table_name="facility_holdings")
    op.drop_table("facility_holdings")
