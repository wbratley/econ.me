"""places and entity location (docs/spatial.md S1)

Revision ID: j6b9d4f2a8c1
Revises: i1f7c8d3e4a5
Create Date: 2026-09-14

The spatial layer's first slice: a places table (opaque refs — key,
kind, region, extent hint, pack provenance; never a coordinate), a
nullable entities.location_place_id (NULL = unplaced, the legacy
citizen: worlds without a map run identically to today), and a nullable
parcels.place_id (a parcel's node on the pack's map — the join S2's
presence gates will read). Dormant by construction: nothing existing
consults a place until packs draw maps (S4).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'j6b9d4f2a8c1'
down_revision: Union[str, Sequence[str], None] = 'i1f7c8d3e4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'places',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('key', sa.String(64), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False, server_default=''),
        sa.Column('description', sa.String(1000), nullable=False,
                  server_default=''),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('region_id', sa.String(64), nullable=False,
                  server_default=''),
        sa.Column('extent_ref', sa.String(255), nullable=True),
        sa.Column('pack_id', sa.String(32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(sa.Column('location_place_id', sa.String(36),
                                      nullable=True))
        batch_op.create_foreign_key('fk_entities_location_place',
                                    'places', ['location_place_id'], ['id'])
    with op.batch_alter_table('parcels') as batch_op:
        batch_op.add_column(sa.Column('place_id', sa.String(36),
                                      nullable=True))
        batch_op.create_foreign_key('fk_parcels_place',
                                    'places', ['place_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('parcels') as batch_op:
        batch_op.drop_constraint('fk_parcels_place', type_='foreignkey')
        batch_op.drop_column('place_id')
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_constraint('fk_entities_location_place', type_='foreignkey')
        batch_op.drop_column('location_place_id')
    op.drop_table('places')
