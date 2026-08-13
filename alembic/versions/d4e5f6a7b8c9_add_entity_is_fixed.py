"""add_entity_is_fixed

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-01 00:00:00.000000

Adds ``entities.is_fixed`` -- the immutable-tier mark of the three-tier
control model (``docs/game.md`` §4, §6).

An economy has three kinds of entity by *who may rewrite their behaviour*:

  * player-owned   -> autonomy (the owner edits freely; ``set_entity_behaviour``)
  * polity-owned   -> legislation (a vote; the existing ``set_script`` path)
  * server, fixed  -> NO ONE, for the epoch (world-physics)

The first two are governed by authorisation (ownership / a capability);
the third refuses both. That refusal needs a mark, and the mark belongs on
the entity -- "this entity's behaviour is operator-set and not editable
through any governed path this epoch" is a property of the entity, not of a
particular script row or a loose WorldSetting list. NPC labourers, the
environment, and any world-physics actor are stamped ``is_fixed=True`` by
the operator at content time (admin API / scenario builder), and both the
autonomy path and the legislation path then refuse to change their
behaviour. The admin path (operator fiat) remains the sole way to set a
fixed entity's script -- which is exactly how it was authored.

Default False: nothing already created is fixed, so existing worlds and
experiments keep working unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.add_column(
            sa.Column('is_fixed', sa.Boolean(), nullable=False, server_default=sa.text('0'))
        )


def downgrade() -> None:
    with op.batch_alter_table('entities') as batch_op:
        batch_op.drop_column('is_fixed')
