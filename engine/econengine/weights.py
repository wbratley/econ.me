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

Per-proposal data rides in the *spec*: a weight-model string is ``name`` or
``name:scope``. The scope is form-specific and answers "which electorate?"
for forms that need it — the symbol for ``share`` (``share:ACME``). Forms
with no per-proposal data (``citizen``) carry no scope. Keeping it in the
spec string matches the stringly-typed intent params and needs no schema
change; it is what makes a joint-stock company, a council, or a weighted
chamber a single new entry plus some data, never a change to the proposal,
vote, or enact ops.

Shipped here:

- *citizen* — direct democracy: every active INDIVIDUAL, weight 1 each.
- *share* — a joint-stock company: holders of a symbol (the cap table),
  weighted by quantity held. Reuses the same holding register
  ``ctx.query.holders`` exposes to scripts; a share changing hands mid-vote
  changes who can vote and how much — exactly as in a real company.

Later forms (council, weighted council, representative, liquid) are more
entries backed by a register/WorldSetting, never new mechanism.
"""

from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity, EntityType, EntityStatus, Holding


def parse_spec(spec: str) -> tuple[str, str]:
    """Split a weight-model spec into ``(name, scope)``.

    ``"citizen"`` → ``("citizen", "")``; ``"share:ACME"`` →
    ``("share", "ACME")``. The scope is the per-proposal data a form needs
    to pin down its electorate (the symbol for ``share``); forms that need
    none carry an empty scope. A bare ``":"``-less spec has no scope.
    """
    name, sep, scope = spec.partition(":")
    return name.strip(), scope


# --- citizen: direct democracy ----------------------------------------------

def _citizen_electorate(session: Session, scope: str) -> dict[str, Decimal]:
    """Direct democracy: every active INDIVIDUAL, weight 1 each.

    Membership is computed from engine state (entity_type + status), not
    from a capability or a token — so a citizen is whoever the world says
    is a living individual. An incapacitated individual (e.g. dead) leaves
    the electorate, which is what makes death withdraw voting power. The
    ``scope`` is unused (direct democracy has no per-proposal data).
    """
    rows = session.execute(
        select(Entity.id).where(
            Entity.entity_type == EntityType.INDIVIDUAL,
            Entity.status == EntityStatus.ACTIVE,
        )
    ).all()
    return {row[0]: Decimal(1) for row in rows}


def _citizen_weight(session: Session, entity_id: str, scope: str) -> Decimal:
    e = session.get(Entity, entity_id)
    if e is None:
        return Decimal(0)
    if e.entity_type != EntityType.INDIVIDUAL:
        return Decimal(0)
    if e.status != EntityStatus.ACTIVE:
        return Decimal(0)
    return Decimal(1)


# --- share: a joint-stock company -------------------------------------------

_SHARE_USAGE = "share weight model needs a symbol: use 'share:SYMBOL'"


def _share_electorate(session: Session, symbol: str) -> dict[str, Decimal]:
    """Holders of ``symbol`` with positive quantity — the cap table.

    The electorate for a corporation: whoever holds shares may vote,
    weighted by shares held. Read live from the holding register (the same
    cap table ``ctx.query.holders`` exposes to scripts), so a share traded
    mid-vote immediately changes the electorate and the weights — there is
    no stale snapshot, exactly as in a real company. Reuses existing engine
    data; there is no separate voting token.
    """
    if not symbol:
        raise ValueError(_SHARE_USAGE)
    rows = session.execute(
        select(Holding.entity_id, Holding.quantity).where(
            Holding.symbol == symbol.upper(), Holding.quantity > 0
        )
    ).all()
    return {str(eid): Decimal(qty) for eid, qty in rows}


def _share_weight(session: Session, entity_id: str, symbol: str) -> Decimal:
    if not symbol:
        raise ValueError(_SHARE_USAGE)
    row = session.execute(
        select(Holding.quantity).where(
            Holding.entity_id == entity_id, Holding.symbol == symbol.upper()
        )
    ).first()
    if row is None or row[0] <= 0:
        return Decimal(0)
    return Decimal(row[0])


#: model name -> (electorate-finder, weight-finder). Each finder takes the
#: per-proposal scope (the symbol for `share`, ignored for `citizen`). Add a
#: row to add a form of government; the proposal/vote/enact ops never change.
WEIGHT_MODELS = {
    "citizen": (_citizen_electorate, _citizen_weight),
    "share": (_share_electorate, _share_weight),
}


def get_model(spec: str):
    """The (electorate-finder, weight-finder) pair named by ``spec``, or
    None if the name is unknown. The scope is *not* validated here — the
    finder raises on use if it needs a scope and got none."""
    name, _ = parse_spec(spec)
    return WEIGHT_MODELS.get(name)


def electorate(session: Session, spec: str) -> dict[str, Decimal]:
    """{entity_id: weight} for every member of the electorate under ``spec``."""
    name, scope = parse_spec(spec)
    pair = WEIGHT_MODELS.get(name)
    if pair is None:
        raise ValueError(f"unknown weight model {name!r}")
    return pair[0](session, scope)


def weight_of(session: Session, spec: str, entity_id: str) -> Decimal:
    """One member's weight under ``spec`` (0 if not in the electorate)."""
    name, scope = parse_spec(spec)
    pair = WEIGHT_MODELS.get(name)
    if pair is None:
        raise ValueError(f"unknown weight model {name!r}")
    return pair[1](session, entity_id, scope)
