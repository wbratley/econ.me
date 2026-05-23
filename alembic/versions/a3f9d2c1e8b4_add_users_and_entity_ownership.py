"""add users and entity ownership

Revision ID: a3f9d2c1e8b4
Revises: 121ab4b5913e
Create Date: 2026-05-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f9d2c1e8b4'
down_revision: Union[str, Sequence[str], None] = '121ab4b5913e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('email', sa.String(255), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('provider_id', sa.String(255), nullable=False),
        sa.Column('is_admin', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(sa.Column('owner_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key('fk_entities_owner_id', 'users', ['owner_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_constraint('fk_entities_owner_id', type_='foreignkey')
        batch_op.drop_column('owner_id')
    op.drop_table('users')
