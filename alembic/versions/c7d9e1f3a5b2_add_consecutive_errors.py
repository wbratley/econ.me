"""crash-revert counter on scripts

Revision ID: c7d9e1f3a5b2
Revises: f7a3c9d2e1b8
Create Date: 2026-08-20 23:30:00.000000

Adds `scripts.consecutive_errors`: how many ticks in a row a BEHAVIOUR
script has crashed at runtime. At CRASH_REVERT_TICKS (3) the engine
deactivates the crasher and re-activates its lineage ancestor, so a
compiling-but-broken submission paralyzes the entity for ticks, not
rounds. Reset to 0 on any successful run and on re-activation.

stone-run6: House Nemotron submitted a script whose line 38 was
`ctx.accounts[0].id` (Lua is 1-indexed; [0] is nil). It compiled clean,
then crashed every tick for 28 straight ticks while HUNGER climbed to
the 15.0 death threshold. The error was fed back; the model's round-3
"fix" was byte-identical. The entity died holding working code in its
lineage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7d9e1f3a5b2'
down_revision: Union[str, Sequence[str], None] = 'f7a3c9d2e1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('scripts') as batch_op:
        batch_op.add_column(
            sa.Column('consecutive_errors', sa.Integer(), nullable=False,
                      server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('scripts') as batch_op:
        batch_op.drop_column('consecutive_errors')
