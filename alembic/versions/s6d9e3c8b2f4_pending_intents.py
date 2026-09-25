"""direct-action outbox: pending_intents

Revision ID: s6d9e3c8b2f4
Revises: q3f8b1c5d2e7
Create Date: 2026-09-24

Controller-authored intents (POST /intents, MCP perform_action, a future
human client) park here when the world clock is armed; run_tick drains
them into the same intent-resolution pass behaviour scripts feed — same
resolver, same priority sort, same per-tick budgets (one say per entity
per tick across both channels), events stamped into the Tick row where
every observer reads them.
"""

revision = "s6d9e3c8b2f4"
down_revision = "q3f8b1c5d2e7"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "pending_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_id", sa.String(36),
                  sa.ForeignKey("entities.id"), nullable=False, index=True),
        sa.Column("intent_type", sa.String(48), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pending_intents")
