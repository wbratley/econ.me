"""widen currency columns

Revision ID: f7a3c9d2e1b8
Revises: d4e5f6a7b8c9
Create Date: 2026-08-16 22:10:00.000000

Widens `accounts.currency` and `markets.currency` from String(3) to
String(8). The ISO 4217 assumption broke the moment a content pack wanted
a 4-char currency ("COIN" in the stone age — found as shiny stones, not
endowed). SQLite never enforced the length, so existing worlds are
unaffected; Postgres would have refused the insert. 8 chars buys room for
named currencies without re-litigating this every pack.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f7a3c9d2e1b8'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('accounts') as batch_op:
        batch_op.alter_column(
            'currency', existing_type=sa.String(3), type_=sa.String(8),
            existing_nullable=False)
    with op.batch_alter_table('markets') as batch_op:
        batch_op.alter_column(
            'currency', existing_type=sa.String(3), type_=sa.String(8),
            existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table('markets') as batch_op:
        batch_op.alter_column(
            'currency', existing_type=sa.String(8), type_=sa.String(3),
            existing_nullable=False)
    with op.batch_alter_table('accounts') as batch_op:
        batch_op.alter_column(
            'currency', existing_type=sa.String(8), type_=sa.String(3),
            existing_nullable=False)
