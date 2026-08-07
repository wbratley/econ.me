"""add_proposal_type

Revision ID: f8d2a1c4b6e9
Revises: e4b7c2f9a3d1
Create Date: 2026-08-03 00:00:00.000000

Adds ``proposals.proposal_type`` — the constitutional tier
(docs/actors.md step 4a-4 / 4b).

A proposal is either ``ordinary`` (the default; mutations are
set_fiscal_policy / set_script) or ``constitutional`` (mutations are
set_validator / set_constitution). The distinction is what gates the
exercise of constitutional power: enacting an ordinary proposal needs the
``legislate`` capability, enacting a constitutional one needs
``amend_constitution`` and must clear the supermajority floor held in the
``constitution`` world setting.

Existing proposals backfill to ``ordinary`` — the only kind that existed
before the tier.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8d2a1c4b6e9'
down_revision: Union[str, Sequence[str], None] = 'e4b7c2f9a3d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'proposals',
        sa.Column(
            'proposal_type',
            sa.Enum('ORDINARY', 'CONSTITUTIONAL', name='proposaltype'),
            nullable=False,
            server_default='ORDINARY',
        ),
    )


def downgrade() -> None:
    op.drop_column('proposals', 'proposal_type')
    sa.Enum(name='proposaltype').drop(op.get_bind(), checkfirst=True)
