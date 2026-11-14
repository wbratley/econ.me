"""Estate escrows: the dead hand holding the estate for the heir

Revision ID: a8f1d4c7b3e2
Revises: z9a4c2e7f1d8
Create Date: 2026-11-10
"""

from alembic import op
import sqlalchemy as sa


revision = "a8f1d4c7b3e2"
down_revision = "z9a4c2e7f1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estate_escrows",
        sa.Column("id", sa.String(36), primary_key=True),
        # owner_id is a platform users.id — no FK: the engine never
        # references platform tables, owner linkage is by string.
        sa.Column("owner_id", sa.String(36), nullable=False, index=True),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("entity_name", sa.String(255), nullable=False,
                  server_default=""),
        sa.Column("created_tick", sa.Integer, nullable=False),
        sa.Column("expires_tick", sa.Integer, nullable=False),
        sa.Column("goods", sa.JSON, nullable=False),
        sa.Column("money", sa.JSON, nullable=False),
        sa.Column("parcels", sa.JSON, nullable=False),
        sa.Column("place_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("estate_escrows")
