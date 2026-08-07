"""Admin read endpoints for the democracy layer (actors step 4a-ii).

Proposals and votes are created through the intent API (POST /intents:
create_proposal / vote / enact), the same resolver the tick engine drains.
These endpoints are the platform's read side — list/detail a proposal,
list its votes — so a UI can render the ballot and the record. Mutation
stays intent-only: there is no bespoke write endpoint to bypass the
capability gates and validators that enactment runs.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from econ.api.deps import get_session, require_admin
from econ.api.schemas import ProposalRead, VoteRead
from econengine.models import Proposal, ProposalStatus, User, Vote

router = APIRouter(prefix="/admin/proposals", tags=["proposals"])


@router.get("", response_model=list[ProposalRead])
def list_proposals(
    status: ProposalStatus | None = Query(default=None),
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    stmt = select(Proposal)
    if status is not None:
        stmt = stmt.where(Proposal.status == status)
    stmt = stmt.order_by(Proposal.created_at, Proposal.id)
    return list(session.execute(stmt).scalars())


@router.get("/{proposal_id}", response_model=ProposalRead)
def get_proposal(
    proposal_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return proposal


@router.get("/{proposal_id}/votes", response_model=list[VoteRead])
def list_votes(
    proposal_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return list(session.execute(
        select(Vote).where(Vote.proposal_id == proposal_id).order_by(Vote.created_at, Vote.id)
    ).scalars())
