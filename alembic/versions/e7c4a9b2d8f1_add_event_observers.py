"""event observers: the witness table

Revision ID: e7c4a9b2d8f1
Revises: d5f2a8c4b6e1
Create Date: 2026-08-23 11:30:00.000000

Speech and witness v1 (game.md 15.6): one table freezing, at emission
time, which entity perceived which event. Derived data — never part of
events_hash. v1 delivery is broadcast of the observable vocabulary
(say, entity_incapacitated) to every active entity; distance and
networks are later rules over the same table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7c4a9b2d8f1"
down_revision: Union[str, None] = "d5f2a8c4b6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_observers",
        sa.Column("tick_number", sa.Integer(), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("observer_id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("tick_number", "event_index", "observer_id"),
    )
    op.create_index(
        op.f("ix_event_observers_observer_id"),
        "event_observers",
        ["observer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_event_observers_observer_id"), table_name="event_observers")
    op.drop_table("event_observers")
