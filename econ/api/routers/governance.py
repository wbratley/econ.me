"""Governance-window endpoints (docs/game.md §14.4; Phase 2b).

The calendar is a derived world fact (``GET /governance/current`` -- public
to authenticated players, MCP-exposed); enactment is the clerk's job, and
the admin convenience here is *his hand, not a second surface*: it runs
the same ``enact`` intent through ``resolve_intent`` as each proposal's
target, so capability gates and VALIDATORs fire exactly as for a live
intent.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.governance import enact_open_proposals, governance_state
from econ.api.schemas import (
    EnactmentOutcome,
    GovernanceEnactBody,
    GovernanceState,
)
from econengine.models import User

router = APIRouter(tags=["governance"])


@router.get("/governance/current", response_model=GovernanceState)
def current(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The governance calendar + docket, for players.

    Is the round open for submission a window round (does resolving it
    close a window, triggering the clerk's enactment sweep)? When is the
    next window? What is dormant on the docket, with live tallies? An
    in-world fact: no side channel, no omniscience -- the same derivation
    any script could make from ``round.state``."""
    return GovernanceState(**governance_state(session))


@router.post("/admin/governance/enact", response_model=list[EnactmentOutcome])
def enact(
    body: GovernanceEnactBody | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Force-run enactment through the ordinary intent path (§14.4).

    A by-election button: without a ``proposal_id`` it sweeps every open
    proposal (what the clerk does at window close); with one, just that
    proposal. Each enactment runs as the proposal's *target* government
    through ``resolve_intent`` -- the tier's capability is checked on that
    entity, VALIDATORs fire on every mutation, and a target without
    LEGISLATE is simply rejected. This endpoint is a hand, not a surface:
    it can never make law the governed path could not.

    Rejections come back as outcome rows (status=rejected), not HTTP
    errors -- the caller wants the ledger of what was tried.
    """
    proposal_id = body.proposal_id if body is not None else None
    if proposal_id is not None:
        from sqlalchemy import select

        from econengine.models import Proposal
        found = session.execute(
            select(Proposal).where(Proposal.id == proposal_id)
        ).scalar_one_or_none()
        if found is None:
            raise HTTPException(status_code=404, detail="unknown proposal")
        if found.status.value != "open":
            raise HTTPException(
                status_code=409,
                detail=f"proposal is {found.status.value}, not open",
            )
    outcomes = enact_open_proposals(session, proposal_id)
    session.commit()
    return [EnactmentOutcome(**o) for o in outcomes]
