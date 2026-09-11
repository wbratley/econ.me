"""facility commons: access, capacity, fuel; good banking cap; recipe fuel

Revision ID: n8f4a1c6e0d3
Revises: m5c1e9f4a7b3
Create Date: 2026-09-20

The fire rework's engine side (ROADMAP P1). Facilities gain commons
semantics: `access` (OWNER default, PLACE = public -- any active entity
may bind; localize with the recipe's presence gates), `capacity`
(concurrent binder slots; the old rule is capacity=1), and a burning
stock (`fuel`, `fuel_capacity`, `fuel_burn_per_tick`) -- stoked up,
burned down, zero is a fire gone dark. Goods gain `max_holding` (the
banking cap: positive credits clip at the choke points -- WARMTH is a
seat by the fire, not a stockpile). Recipes gain `facility_fuel_output`
(stoking: credit fuel to the bound facility at completion, refused at
start when the facility is fully banked) and `builds_facility_access`
(erect a public facility). All defaults reproduce today's behavior.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'n8f4a1c6e0d3'
down_revision: Union[str, Sequence[str], None] = 'm5c1e9f4a7b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('facilities', sa.Column(
        'access', sa.String(length=16), nullable=False,
        server_default='OWNER'))
    op.add_column('facilities', sa.Column(
        'capacity', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('facilities', sa.Column(
        'fuel', sa.Numeric(precision=18, scale=4), nullable=False,
        server_default='0'))
    op.add_column('facilities', sa.Column(
        'fuel_capacity', sa.Numeric(precision=18, scale=4), nullable=True))
    op.add_column('facilities', sa.Column(
        'fuel_burn_per_tick', sa.Numeric(precision=18, scale=4),
        nullable=False, server_default='0'))
    op.add_column('goods', sa.Column(
        'max_holding', sa.Numeric(precision=18, scale=4), nullable=True))
    op.add_column('recipes', sa.Column(
        'facility_fuel_output', sa.Numeric(precision=18, scale=4),
        nullable=True))
    op.add_column('recipes', sa.Column(
        'requires_facility_lit', sa.Boolean(), nullable=False,
        server_default='false'))
    op.add_column('recipes', sa.Column(
        'builds_facility_config', sa.JSON(), nullable=True))
    op.add_column('recipes', sa.Column(
        'builds_facility_access', sa.String(length=16), nullable=False,
        server_default='OWNER'))


def downgrade() -> None:
    op.drop_column('recipes', 'builds_facility_access')
    op.drop_column('recipes', 'builds_facility_config')
    op.drop_column('recipes', 'requires_facility_lit')
    op.drop_column('recipes', 'facility_fuel_output')
    op.drop_column('goods', 'max_holding')
    op.drop_column('facilities', 'fuel_burn_per_tick')
    op.drop_column('facilities', 'fuel_capacity')
    op.drop_column('facilities', 'fuel')
    op.drop_column('facilities', 'capacity')
    op.drop_column('facilities', 'access')
