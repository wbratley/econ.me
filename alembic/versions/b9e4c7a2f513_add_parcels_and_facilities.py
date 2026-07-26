"""add_parcels_and_facilities

Adds land (parcels, facilities, deposits), the present-but-not-consumed
recipe requirement tables (recipe_good_requirements, recipe_deposit_inputs),
the facility columns on recipes (requires_facility / builds_facility), and
parcel binding on processes (processes.parcel_id).

Revision ID: b9e4c7a2f513
Revises: a8d5f2e7c419
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9e4c7a2f513'
down_revision: Union[str, Sequence[str], None] = 'a8d5f2e7c419'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('parcels',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('parcel_type', sa.String(length=32), nullable=False),
    sa.Column('region_id', sa.String(length=64), nullable=False),
    sa.Column('extent_ref', sa.String(length=255), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['entities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('facilities',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('parcel_id', sa.String(length=36), nullable=False),
    sa.Column('facility_type', sa.String(length=32), nullable=False),
    sa.Column('built_tick', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parcel_id'], ['parcels.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_table('deposits',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('parcel_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('capacity', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('regen_per_tick', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parcel_id'], ['parcels.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('parcel_id', 'symbol'),
    )
    op.create_table('recipe_good_requirements',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'symbol'),
    )
    op.create_table('recipe_deposit_inputs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'symbol'),
    )
    op.add_column('recipes', sa.Column('requires_facility', sa.String(length=32), nullable=True))
    op.add_column('recipes', sa.Column('builds_facility', sa.String(length=32), nullable=True))
    op.add_column('processes', sa.Column('parcel_id', sa.String(length=36), nullable=True))
    with op.batch_alter_table('processes') as batch_op:
        batch_op.create_foreign_key('fk_processes_parcel_id', 'parcels', ['parcel_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('processes') as batch_op:
        batch_op.drop_constraint('fk_processes_parcel_id', type_='foreignkey')
    op.drop_column('processes', 'parcel_id')
    op.drop_column('recipes', 'builds_facility')
    op.drop_column('recipes', 'requires_facility')
    op.drop_table('recipe_deposit_inputs')
    op.drop_table('recipe_good_requirements')
    op.drop_table('deposits')
    op.drop_table('facilities')
    op.drop_table('parcels')
