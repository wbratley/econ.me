"""add_proposals_votes

Revision ID: e4b7c2f9a3d1
Revises: d2c5e8a1f7b4
Create Date: 2026-08-02 00:00:00.000000

Adds the ``proposals`` and ``votes`` tables — the proposal→vote→enact
democracy layer (docs/actors.md step 4a-ii).

A ``proposal`` is a batch of proposed mutations (set_fiscal_policy and/or
set_script) plus a weight model, threshold, and quorum; it is inert until
``enact`` applies it, and only if the vote passed. A ``vote`` is one
entity's for/against on one proposal, with the voter's weight snapshotted
at cast time by the weight-model resolver (``engine/econengine/weights.py``).
One vote per voter per proposal is enforced by a unique constraint.

The "form of government" is data on the proposal (weight_model + threshold
+ quorum), never mechanism; the same two tables serve direct democracy,
a corporation, a council, or representation — each a different resolver
entry. This migration ships the tables only; ``citizen`` is the sole
resolver in 4a-ii.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4b7c2f9a3d1'
down_revision: Union[str, Sequence[str], None] = 'd2c5e8a1f7b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'proposals',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('proposer_id', sa.String(length=36), nullable=False),
        sa.Column('target_id', sa.String(length=36), nullable=False),
        sa.Column('weight_model', sa.String(length=64), nullable=False),
        sa.Column('threshold', sa.String(length=32), nullable=False, server_default='0.5'),
        sa.Column('quorum', sa.String(length=32), nullable=False, server_default='0'),
        sa.Column('mutations', sa.JSON(), nullable=False),
        sa.Column('status', sa.Enum('OPEN', 'ENACTED', 'FAILED', 'CANCELLED', name='proposalstatus'),
                  nullable=False, server_default='OPEN'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('enacted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tally_yes', sa.String(length=64), nullable=True),
        sa.Column('tally_no', sa.String(length=64), nullable=True),
        sa.Column('tally_electorate', sa.String(length=64), nullable=True),
        sa.Column('tally_turnout', sa.String(length=64), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['proposer_id'], ['entities.id']),
        sa.ForeignKeyConstraint(['target_id'], ['entities.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_proposals_status', 'proposals', ['status'])
    op.create_index('ix_proposals_target_id', 'proposals', ['target_id'])

    op.create_table(
        'votes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('proposal_id', sa.String(length=36), nullable=False),
        sa.Column('voter_id', sa.String(length=36), nullable=False),
        sa.Column('choice', sa.Enum('FOR', 'AGAINST', name='votechoice'), nullable=False),
        sa.Column('weight', sa.String(length=64), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['proposal_id'], ['proposals.id']),
        sa.ForeignKeyConstraint(['voter_id'], ['entities.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('proposal_id', 'voter_id', name='uq_votes_proposal_voter'),
    )
    op.create_index('ix_votes_proposal_id', 'votes', ['proposal_id'])


def downgrade() -> None:
    op.drop_index('ix_votes_proposal_id', table_name='votes')
    op.drop_table('votes')
    op.drop_index('ix_proposals_target_id', table_name='proposals')
    op.drop_index('ix_proposals_status', table_name='proposals')
    op.drop_table('proposals')
    # drop the enum types created for these tables (batch mode / postgres)
    sa.Enum(name='votechoice').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='proposalstatus').drop(op.get_bind(), checkfirst=True)
