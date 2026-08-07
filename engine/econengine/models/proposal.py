"""Proposal + Vote — the proposal→vote→enact democracy layer (actors step 4a-ii).

A ``Proposal`` bundles a batch of proposed mutations (``set_fiscal_policy``
and/or ``set_script``) with a weight model, a threshold, and a quorum. It
is inert until enacted: creating it and voting on it change nothing. Only
``enact`` applies the mutations, and only if the vote passed — and even
then each mutation runs through ``resolve_intent`` as the target
government, so capability gates and VALIDATORs fire exactly as for a live
intent. That is the safety thesis (docs/actors.md, "voting on code,
safely"): a citizen-enacted law has exactly the powers and limits of one
an operator pasted in.

A ``Vote`` records one entity's choice on one proposal, with the voter's
weight snapshotted at cast time (computed by the weight-model resolver,
not self-declared). One vote per voter per proposal (unique constraint).

The "form of government" is data on the proposal (``weight_model`` +
``threshold`` + ``quorum``), not mechanism: the same proposal/vote/enact
ops serve direct democracy, a corporation, a council, or representation
— each is a different resolver entry in ``weights.WEIGHT_MODELS`` (4a-ii
ships only ``citizen``).
"""
import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import (
    String, Text, DateTime, JSON, ForeignKey, UniqueConstraint, Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class ProposalStatus(enum.Enum):
    OPEN = "open"            # accepting votes; awaiting enactment
    ENACTED = "enacted"      # passed threshold + quorum; mutations applied
    FAILED = "failed"        # tallied but did not pass, or a mutation was vetoed/rejected
    CANCELLED = "cancelled"  # withdrawn by the proposer before enactment


class ProposalType(enum.Enum):
    # The tier a proposal sits in. ORDINARY mutations (set_fiscal_policy /
    # set_script) are ordinary law — enacting needs `legislate` and the
    # proposal's own threshold/quorum. CONSTITUTIONAL mutations
    # (set_validator / set_constitution) amend the constitution — enacting
    # needs `amend_constitution` and must clear the supermajority floor
    # held in the `constitution` world setting. The tier is checked at
    # propose time (an ordinary proposal cannot carry a constitutional
    # mutation, and vice versa) so the two surfaces never cross.
    ORDINARY = "ordinary"
    CONSTITUTIONAL = "constitutional"


class VoteChoice(enum.Enum):
    FOR = "for"
    AGAINST = "against"


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # ordinary law vs. a constitutional amendment — gates which mutations
    # are allowed (checked at propose) and which capability + floor the
    # enactment needs (checked at enact). See ProposalType above.
    proposal_type: Mapped[ProposalType] = mapped_column(
        SAEnum(ProposalType), nullable=False, default=ProposalType.ORDINARY
    )
    # the citizen who proposed (must be in the electorate at propose time)
    proposer_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    # the government that enacts — holds the capabilities the mutations need,
    # and is the entity the mutations run as on enactment.
    target_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    # which weight-model resolver computes the electorate + weights
    # (a key into weights.WEIGHT_MODELS, e.g. "citizen")
    weight_model: Mapped[str] = mapped_column(String(64), nullable=False)
    # fraction of cast weight that must be FOR (e.g. "0.5" = simple majority)
    threshold: Mapped[str] = mapped_column(String(32), nullable=False, default="0.5")
    # fraction of the electorate that must cast a vote (e.g. "0.1")
    quorum: Mapped[str] = mapped_column(String(32), nullable=False, default="0")
    # the proposed mutations: a JSON list of {type, params} — each is an
    # intent applied (as the target) on enactment via resolve_intent, so
    # capability gates and validators fire exactly as for a live intent.
    # params are stringly typed, same as intent params.
    mutations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[ProposalStatus] = mapped_column(
        SAEnum(ProposalStatus), nullable=False, default=ProposalStatus.OPEN
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    enacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # tallied outcome, denormalised once enacted/failed for audit
    tally_yes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tally_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tally_electorate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tally_turnout: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    proposer: Mapped["Entity"] = relationship("Entity", foreign_keys=[proposer_id])
    target: Mapped["Entity"] = relationship("Entity", foreign_keys=[target_id])
    votes: Mapped[list["Vote"]] = relationship(
        "Vote", back_populates="proposal", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Proposal id={self.id} title={self.title!r} status={self.status.value}>"


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("proposal_id", "voter_id", name="uq_votes_proposal_voter"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    proposal_id: Mapped[str] = mapped_column(String(36), ForeignKey("proposals.id"), nullable=False)
    voter_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    choice: Mapped[VoteChoice] = mapped_column(SAEnum(VoteChoice), nullable=False)
    # the voter's weight at cast time, snapshotted by the resolver so the
    # record shows exactly what was counted. For the citizen model, "1".
    weight: Mapped[str] = mapped_column(String(64), nullable=False, default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    proposal: Mapped["Proposal"] = relationship("Proposal", back_populates="votes")
    voter: Mapped["Entity"] = relationship("Entity", foreign_keys=[voter_id])

    def __repr__(self) -> str:
        return f"<Vote proposal={self.proposal_id} voter={self.voter_id} choice={self.choice.value}>"
