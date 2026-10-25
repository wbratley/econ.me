"""Process effort factor: the fraction of declared LABOR paid at start

Revision ID: z9a4c2e7f1d8
Revises: y7e2f4a8c1b5
Create Date: 2025-01-06
"""

from alembic import op
import sqlalchemy as sa


revision = "z9a4c2e7f1d8"
down_revision = "y7e2f4a8c1b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processes",
        sa.Column(
            "effort_factor",
            sa.Numeric(precision=18, scale=4),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("processes", "effort_factor")
