"""presence gates (docs/spatial.md S2)

Revision ID: k7c0e5a3b9d2
Revises: j6b9d4f2a8c1
Create Date: 2026-09-14

The spatial layer's second slice: WHERE an entity stands may gate WHAT
it may do. Recipes gain two nullable presence columns —
requires_place_kind ("any HEARTH") and requires_place_key ("that exact
river", stored as the pack key: data, resolved at gate time, so catalog
recipes stay installable on worlds that never draw the map) — checked
in start_process's requirement pass; markets gain a nullable place_id
seat (NULL = the global market of today, Fork 5) checked in
place_order. Gates fire only on declared data: every legacy recipe and
market runs exactly as before. No new event types — rejection reasons
carry the place names.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'k7c0e5a3b9d2'
down_revision: Union[str, Sequence[str], None] = 'j6b9d4f2a8c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('recipes') as batch_op:
        batch_op.add_column(sa.Column('requires_place_kind', sa.String(32),
                                      nullable=True))
        batch_op.add_column(sa.Column('requires_place_key', sa.String(64),
                                      nullable=True))
    with op.batch_alter_table('markets') as batch_op:
        batch_op.add_column(sa.Column('place_id', sa.String(36),
                                      nullable=True))
        batch_op.create_foreign_key('fk_markets_place',
                                    'places', ['place_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('markets') as batch_op:
        batch_op.drop_constraint('fk_markets_place', type_='foreignkey')
        batch_op.drop_column('place_id')
    with op.batch_alter_table('recipes') as batch_op:
        batch_op.drop_column('requires_place_key')
        batch_op.drop_column('requires_place_kind')
