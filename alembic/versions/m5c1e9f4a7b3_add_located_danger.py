"""located danger (docs/spatial.md S4)

Revision ID: m5c1e9f4a7b3
Revises: l9e2b5f7d1c8
Create Date: 2026-09-16

The spatial layer's fourth slice: danger gets an address.
threats.place_id, nullable — NULL keeps the ambient everywhere-threat
of the pre-spatial worlds (legacy behavior, pinned by test); a placed
threat pressures only the entities its world says are there (standing
at the spot, or mid-hop on a road that started there — a traveller
stands at the hop's origin until arrival moves them). Home ranges are
pack data, installed with the map. Combat co-location and prowling
scope read the same rows; no schema of theirs changes here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'm5c1e9f4a7b3'
down_revision: Union[str, Sequence[str], None] = 'l9e2b5f7d1c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('threats') as batch_op:
        batch_op.add_column(sa.Column('place_id', sa.String(36),
                                      nullable=True))
        batch_op.create_foreign_key('fk_threats_place',
                                    'places', ['place_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('threats') as batch_op:
        batch_op.drop_constraint('fk_threats_place', type_='foreignkey')
        batch_op.drop_column('place_id')
