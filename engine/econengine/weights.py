"""Pluggable vote-weight models — the "form of government" as data.

A form of government is three pieces of data, never new mechanism
(docs/actors.md, "Forms of government are data, not mechanism"):

  1. the electorate — who may vote,
  2. the weight function — how much each vote counts,
  3. the threshold — how much weight "yes" needs to enact.

This module is (1)+(2): a registry mapping a model name to a pair of
functions — an electorate-finder and a weight-finder — both backed by
existing engine data, so there is no separate voting token. The
proposal→vote→enact machinery (step 4a-ii) is form-agnostic; it asks this
registry "who votes, and how much?" and applies the result.

The citizen model (shipped here) is direct democracy: every active
INDIVIDUAL is in the electorate with weight 1. Adding a corporation
(share-weight, reusing the cap table via ctx.query.holders), a council, a
weighted council, or representation is a new entry in WEIGHT_MODELS plus
whatever register/WorldSetting backs it — never a change to the proposal,
vote, or enact ops. That is what makes "vote on code" a data-only
extension to a monarchy, oligarchy, or joint-stock company.
"""

from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity, EntityType, EntityStatus


def _citizen_electorate(session: Session) -> dict[str, Decimal]:
    """Direct democracy: every active INDIVIDUAL, weight 1 each.

    Membership is computed from engine state (entity_type + status), not
    from a capability or a token — so a citizen is whoever the world says
    is a living individual. An incapacitated individual (e.g. dead) leaves
    the electorate, which is what makes death withdraw voting power.
    """
    rows = session.execute(
        select(Entity.id).where(
            Entity.entity_type == EntityType.INDIVIDUAL,
            Entity.status == EntityStatus.ACTIVE,
        )
    ).all()
    return {row[0]: Decimal(1) for row in rows}


def _citizen_weight(session: Session, entity_id: str) -> Decimal:
    e = session.get(Entity, entity_id)
    if e is None:
        return Decimal(0)
    if e.entity_type != EntityType.INDIVIDUAL:
        return Decimal(0)
    if e.status != EntityStatus.ACTIVE:
        return Decimal(0)
    return Decimal(1)


#: model name -> (electorate-finder, weight-finder). Add a row to add a
#: form of government; the proposal/vote/enact ops never change.
WEIGHT_MODELS = {
    "citizen": (_citizen_electorate, _citizen_weight),
}


def get_model(name: str):
    """The (electorate-finder, weight-finder) pair for `name`, or None."""
    return WEIGHT_MODELS.get(name)


def electorate(session: Session, name: str) -> dict[str, Decimal]:
    """{entity_id: weight} for every member of the electorate under `name`."""
    pair = WEIGHT_MODELS.get(name)
    if pair is None:
        raise ValueError(f"unknown weight model {name!r}")
    return pair[0](session)


def weight_of(session: Session, name: str, entity_id: str) -> Decimal:
    """One member's weight under `name` (0 if not in the electorate)."""
    pair = WEIGHT_MODELS.get(name)
    if pair is None:
        raise ValueError(f"unknown weight model {name!r}")
    return pair[1](session, entity_id)
