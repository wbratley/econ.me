"""topology and travel (docs/spatial.md S3)

Revision ID: l9e2b5f7d1c8
Revises: k7c0e5a3b9d2
Create Date: 2026-09-15

The spatial layer's third slice: the map grows roads and the roads
carry work. `spatial_edges` are weighted, mode-tagged connections
between places (distance is always ticks-through-topology, never
meters); `travel_routes` are the auditable itineraries behind
travel-as-Process; `processes` gains is_travel/edge_id/route_id so a
hop is an ordinary Process the road re-timed (completes_tick = start +
edge cost) whose completion moves the traveller instead of producing.
A world with no edges has no topology — every journey is refused with
a readable reason, and everything else runs exactly as before.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'l9e2b5f7d1c8'
down_revision: Union[str, Sequence[str], None] = 'k7c0e5a3b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'spatial_edges',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('from_place_id', sa.String(36), nullable=False),
        sa.Column('to_place_id', sa.String(36), nullable=False),
        sa.Column('mode', sa.String(32), nullable=False),
        sa.Column('cost_ticks', sa.Integer(), nullable=False),
        sa.Column('bidirectional', sa.Boolean(), nullable=False),
        sa.Column('region_id', sa.String(64), nullable=False,
                  server_default=''),
        sa.Column('pack_id', sa.String(32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['from_place_id'], ['places.id'],
                                name='fk_spatial_edges_from_place'),
        sa.ForeignKeyConstraint(['to_place_id'], ['places.id'],
                                name='fk_spatial_edges_to_place'),
    )
    op.create_table(
        'travel_routes',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('entity_id', sa.String(36), nullable=False),
        sa.Column('destination_place_id', sa.String(36), nullable=False),
        sa.Column('modes', sa.JSON(), nullable=False),
        sa.Column('hops', sa.JSON(), nullable=False),
        sa.Column('next_index', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('ACTIVE', 'ARRIVED', 'STRANDED',
                                    name='travelroutestatus'),
                  nullable=False),
        sa.Column('current_process_id', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'],
                                name='fk_travel_routes_entity'),
        sa.ForeignKeyConstraint(['destination_place_id'], ['places.id'],
                                name='fk_travel_routes_destination'),
        sa.ForeignKeyConstraint(['current_process_id'], ['processes.id'],
                                name='fk_travel_routes_current_process'),
    )
    with op.batch_alter_table('processes') as batch_op:
        batch_op.add_column(sa.Column('is_travel', sa.Boolean(),
                                      nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column('edge_id', sa.String(36),
                                      nullable=True))
        batch_op.add_column(sa.Column('route_id', sa.String(36),
                                      nullable=True))
        batch_op.create_foreign_key('fk_processes_edge',
                                    'spatial_edges', ['edge_id'], ['id'])
        batch_op.create_foreign_key('fk_processes_route',
                                    'travel_routes', ['route_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('processes') as batch_op:
        batch_op.drop_constraint('fk_processes_route', type_='foreignkey')
        batch_op.drop_constraint('fk_processes_edge', type_='foreignkey')
        batch_op.drop_column('route_id')
        batch_op.drop_column('edge_id')
        batch_op.drop_column('is_travel')
    op.drop_table('travel_routes')
    op.drop_table('spatial_edges')
