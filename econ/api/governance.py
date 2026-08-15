"""Governance windows -- the lawmaking cadence (docs/game.md §14.4).

**The window is derived, never stored.** Round ``r`` is a window round iff
``r % N == 0``, where N is *pace, not world-kind* -- deployment config
(``ECON_ROUNDS_PER_WINDOW`` env, default 5), exactly like K. Nothing
persisted says "a window happened"; anyone (platform, script, player) can
re-derive the calendar from the round counter.

**Cadence bites at enactment, never at speech.** The engine does not
time-gate ``create_proposal`` or ``vote`` -- an out-of-window proposal is
legal but dormant, waiting for a window close. *When laws may pass* is
policy; *how laws pass* is mechanism, and mechanism is untouched.

**Enactment is the clerk's job.** The content pack ships a clerk: a
server-owned polity entity holding LEGISLATE whose POLICY script reads
``round.state`` (a WorldSetting scripts already read) and, on window
rounds, enacts open proposals via the ordinary ``enact`` intent. The admin
convenience here runs the *same* enactment through the *same* intent path
(``scripting.resolve_intent`` as the proposal's target) -- a by-election
button, not a second law-making surface: capability gates and VALIDATORs
fire exactly as for a live intent.
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from econ.api.rounds import current_round_state
from econengine.models import Proposal, ProposalStatus, WorldSetting

DEFAULT_ROUNDS_PER_WINDOW = 5


def rounds_per_window() -> int:
    """N -- rounds per governance window. Deployment config (env), default 5.

    Like K (``ECON_TICKS_PER_ROUND``): pace is an operator/deployment knob,
    not world content. Read at call time; falls back to the default if
    unset, blank, or non-positive.
    """
    raw = os.environ.get("ECON_ROUNDS_PER_WINDOW")
    if not raw:
        return DEFAULT_ROUNDS_PER_WINDOW
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS_PER_WINDOW
    return n if n > 0 else DEFAULT_ROUNDS_PER_WINDOW


def is_window_round(round_number: int) -> bool:
    """Round ``r`` is a window round iff ``r % N == 0`` (pure derivation)."""
    return round_number % rounds_per_window() == 0


def next_window_round(round_number: int) -> int:
    """The first window round at or after ``round_number``."""
    n = rounds_per_window()
    return round_number + ((n - round_number % n) % n)


def _live_tally(session: Session, proposal: Proposal) -> dict[str, str]:
    """Yes/no/turnout computed now -- the same math as ``ctx.query.tally``,
    for players watching an open proposal between windows."""
    from decimal import Decimal

    from econengine import weights
    from econengine.models import VoteChoice

    yes = sum(
        (Decimal(v.weight) for v in proposal.votes if v.choice == VoteChoice.FOR),
        Decimal(0),
    )
    no = sum(
        (Decimal(v.weight) for v in proposal.votes if v.choice == VoteChoice.AGAINST),
        Decimal(0),
    )
    electorate = sum(
        weights.electorate(session, proposal.weight_model).values(), Decimal(0)
    )
    cast = yes + no
    turnout = (cast / electorate) if electorate > 0 else Decimal(0)
    return {
        "yes": str(yes),
        "no": str(no),
        "electorate": str(electorate),
        "turnout": str(turnout),
    }


def governance_state(session: Session) -> dict[str, Any]:
    """The governance calendar + the docket, as a pure read.

      * ``rounds_per_window``  -- N (deployment config)
      * ``current_round``      -- the round open for submission
      * ``is_window_round``    -- resolving the current round CLOSES a
                                  window (the clerk will enact)
      * ``next_window_round``  -- the next window-closing round number
      * ``rounds_until_window``-- how many advances until it
      * ``open_proposals``     -- the docket: open proposals with live
                                  tallies (dormant until a window closes)

    An in-world fact: public to authenticated players, MCP-exposed.
    """
    clock = current_round_state(session)
    current = int(clock["current_round"])
    nxt = next_window_round(current)
    open_proposals = []
    for p in session.execute(
        select(Proposal).where(Proposal.status == ProposalStatus.OPEN)
        .order_by(Proposal.created_at, Proposal.id)
    ).scalars():
        open_proposals.append({
            "id": p.id,
            "title": p.title,
            "proposal_type": p.proposal_type.value,
            "proposer_id": p.proposer_id,
            "target_id": p.target_id,
            "weight_model": p.weight_model,
            "threshold": p.threshold,
            "quorum": p.quorum,
            "tally": _live_tally(session, p),
        })
    return {
        "round_number": int(clock["round_number"]),
        "current_round": current,
        "rounds_per_window": rounds_per_window(),
        "is_window_round": is_window_round(current),
        "next_window_round": nxt,
        "rounds_until_window": nxt - current,
        "open_proposals": open_proposals,
    }


def enact_open_proposals(session: Session, proposal_id: str | None = None) -> list[dict]:
    """Run enactment through the ordinary intent path (admin convenience).

    For each open proposal (or just the named one), build the same ``enact``
    intent the clerk's POLICY script would queue and resolve it *as the
    proposal's target* -- ``resolve_intent``'s enact branch checks the
    tier's capability (legislate / amend_constitution) on that entity and
    fires VALIDATORs on every mutation. There is no bypass here: a target
    without the capability is simply rejected, a vetoed mutation fails the
    whole enactment. Returns each intent's outcome event, applied or
    rejected.
    """
    from econengine import scripting
    from econengine.lua_engine import Intent

    stmt = select(Proposal).where(Proposal.status == ProposalStatus.OPEN)
    if proposal_id is not None:
        stmt = stmt.where(Proposal.id == proposal_id)
    proposals = list(session.execute(
        stmt.order_by(Proposal.created_at, Proposal.id)
    ).scalars())

    outcomes: list[dict] = []
    for proposal in proposals:
        intent = Intent(
            entity_id=proposal.target_id,
            intent_type="enact",
            params={"proposal_id": proposal.id, "reference": "admin:governance-enact"},
            resource_ids=[proposal.id],
        )
        outcomes.append(scripting.resolve_intent(session, intent))
    return outcomes
