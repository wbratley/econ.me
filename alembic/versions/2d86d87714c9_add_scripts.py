"""add_scripts

Revision ID: 2d86d87714c9
Revises: c5d8e2f1a047
Create Date: 2026-05-24 12:24:26.657280

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2d86d87714c9'
down_revision: Union[str, Sequence[str], None] = 'c5d8e2f1a047'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('scripts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=1000), nullable=False),
    sa.Column('script_type', sa.Enum('POLICY', 'BEHAVIOUR', 'HOOK', 'VALIDATOR', name='scripttype'), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('timeout_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('scripts')
