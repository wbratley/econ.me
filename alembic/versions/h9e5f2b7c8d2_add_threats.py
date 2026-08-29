"""add threats table

Run ID: stone run 20 -- wolves. The notes' predator finally lands: night
encounters keyed on say-noise, deterred by a lit hearth, answered with
weapons. Threats are declared content rows (like needs): the pack says
what circles in the dark, how much a shout carries, and what holding
keeps it shy; the engine only does the arithmetic. Pressure credits a
condition holding — so the whole existing conditions machinery (decay
as the pack losing interest, thresholds as being eaten, recipes that
consume the condition as fighting back) works on it unchanged.

Revision ID: h9e5f2b7c8d2
Revises: g8d3f0a5c7e1
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'h9e5f2b7c8d2'
down_revision: Union[str, Sequence[str], None] = 'g8d3f0a5c7e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'threats',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('code', sa.String(32), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False, server_default=''),
        sa.Column('description', sa.String(1000), nullable=False,
                  server_default=''),
        sa.Column('pack_id', sa.String(32), nullable=True),
        sa.Column('entity_type', sa.Enum('INDIVIDUAL', 'BUSINESS', 'BANK',
                                         'GOVERNMENT', name='entitytype'),
                  nullable=True),
        sa.Column('condition_symbol', sa.String(32), nullable=False),
        sa.Column('ambient_night_per_tick', sa.Numeric(precision=18,
                                                       scale=4),
                  nullable=False),
        sa.Column('per_say_night', sa.Numeric(precision=18, scale=4),
                  nullable=False, server_default='0'),
        sa.Column('deterred_by_symbol', sa.String(32), nullable=True),
        sa.Column('deterred_by_quantity', sa.Numeric(precision=18,
                                                     scale=4),
                  nullable=False, server_default='0'),
        sa.Column('deterrence_factor', sa.Numeric(precision=5, scale=4),
                  nullable=False, server_default='1'),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('threats')
