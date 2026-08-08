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
- *council* / *weighted* — a council: the members of a named register
  (``councils.py``, a WorldSetting), weight 1 each under ``council`` or the
  declared per-member weight under ``weighted``. ``weighted`` subsumes a
  representative chamber (set each MP's weight to their constituency size).
- *liquid* — liquid democracy: every active INDIVIDUAL, weight 1 each
  plus the weight delegated *to* them (resolved transitively against a
  delegation graph in ``delegations.py``). A delegator leaves the
  electorate (they voted by redirecting); an empty graph is plain direct
  democracy.
"""

from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity, EntityType, EntityStatus, Holding
from . import councils, delegations


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


# --- council / weighted: a named membership register ---------------------

_COUNCIL_USAGE = (
    "council/weighted models need a register name: use 'council:NAME' "
    "or 'weighted:NAME'"
)


def _council_electorate(session: Session, name: str) -> dict[str, Decimal]:
    """The members of council ``name`` — every member, weight 1 each.

    Membership comes from the authored register (``councils.get_register``,
    a WorldSetting), filtered to entities that exist and are ACTIVE — so a
    council member who dies or is incapacitated leaves the electorate, just
    as a citizen does. The scope is the council's name (``council:senate``
    → ``senate``). The register's declared weights are ignored here: this is
    the equal-weight form (oligarchy / senate).
    """
    return _register_electorate(session, name, weighted=False)


def _weighted_electorate(session: Session, name: str) -> dict[str, Decimal]:
    """The members of council ``name``, weighted by their declared weight.

    Same electorate as ``council`` (the same register), but each member's
    weight is the value declared in the register rather than 1. This is a
    weighted council — and it subsumes a *representative* chamber: set each
    MP's weight to their constituency size and the majority is of
    represented population, not of heads.
    """
    return _register_electorate(session, name, weighted=True)


def _register_electorate(
    session: Session, name: str, *, weighted: bool
) -> dict[str, Decimal]:
    """Shared electorate for council/weighted: register members that are
    real, ACTIVE entities. ``weighted`` selects the declared weight vs 1."""
    if not name:
        raise ValueError(_COUNCIL_USAGE)
    register = councils.get_register(session, name)
    if not register:
        return {}
    rows = (
        session.execute(
            select(Entity.id).where(
                Entity.id.in_(list(register)),
                Entity.status == EntityStatus.ACTIVE,
            )
        )
        .scalars()
        .all()
    )
    if weighted:
        return {eid: councils.member_weight(session, name, eid) for eid in rows}
    return {eid: Decimal(1) for eid in rows}


def _council_weight(session: Session, entity_id: str, name: str) -> Decimal:
    if not name:
        raise ValueError(_COUNCIL_USAGE)
    if not _member_active(session, entity_id, name):
        return Decimal(0)
    return Decimal(1)


def _weighted_weight(session: Session, entity_id: str, name: str) -> Decimal:
    if not name:
        raise ValueError(_COUNCIL_USAGE)
    if not _member_active(session, entity_id, name):
        return Decimal(0)
    return councils.member_weight(session, name, entity_id)


def _member_active(session: Session, entity_id: str, name: str) -> bool:
    """Is ``entity_id`` a member of council ``name`` and currently ACTIVE?"""
    register = councils.get_register(session, name)
    if entity_id not in register:
        return False
    e = session.get(Entity, entity_id)
    return e is not None and e.status == EntityStatus.ACTIVE


# --- liquid: direct democracy plus transitive delegation -------------------

_LIQUID_USAGE = "liquid weight model needs a polity name: use 'liquid:NAME'"


def _active_individual_ids(session: Session) -> set[str]:
    """The base electorate pool for liquid (and citizen): every active
    INDIVIDUAL. Liquid layers a delegation graph *over* this same pool, so
    an empty graph is identical to direct democracy."""
    return set(
        session.execute(
            select(Entity.id).where(
                Entity.entity_type == EntityType.INDIVIDUAL,
                Entity.status == EntityStatus.ACTIVE,
            )
        )
        .scalars()
        .all()
    )


def _liquid_resolve_weights(
    active_ids: set[str], delegations: dict[str, str]
) -> dict[str, Decimal]:
    """Resolve each active individual's unit of weight to the *terminal*
    voter at the end of their delegation chain.

    The terminal of a chain is the first member who does not delegate
    (``delegations.get(cur) is None``). A delegator therefore leaves the
    electorate (their weight is redirected, not cast by them). Three
    things strand a unit of weight — it is dropped rather than counted, a
    fail-safe that flags a broken graph instead of inflating anyone:

      - a *cycle* (A→B→A): the chain never reaches a non-delegator;
      - a delegation *outside the pool* (to an inactive or non-individual
        entity): the chain leaves the active electorate;
      - (defensively) a self-loop, which is a 1-cycle — also rejected at
        set time.

    Returns ``{terminal_id: accumulated_weight}`` — the electorate under
    ``liquid:{name}``.
    """
    weight: dict[str, Decimal] = {}
    for person in active_ids:
        seen = {person}
        cur = person
        terminal = None
        while True:
            nxt = delegations.get(cur)
            if nxt is None:
                terminal = cur          # cur does not delegate → terminal
                break
            if nxt not in active_ids:
                terminal = None         # chain leaves the active electorate
                break
            if nxt in seen:
                terminal = None         # cycle
                break
            seen.add(nxt)
            cur = nxt
        if terminal is not None:
            weight[terminal] = weight.get(terminal, Decimal(0)) + Decimal(1)
    return weight


def _liquid_electorate(session: Session, name: str) -> dict[str, Decimal]:
    """Liquid democracy over the active individuals of polity ``name``.

    The base pool is every active INDIVIDUAL (as in direct democracy); the
    delegation graph (``delegations.get_delegations``, a WorldSetting) is
    layered over it. Each member's weight is 1 plus the weight delegated to
    them transitively. A delegator is not in the electorate — they voted by
    redirecting. With an empty graph this is identical to ``citizen``.
    """
    if not name:
        raise ValueError(_LIQUID_USAGE)
    return _liquid_resolve_weights(
        _active_individual_ids(session),
        delegations.get_delegations(session, name),
    )


def _liquid_weight(session: Session, entity_id: str, name: str) -> Decimal:
    if not name:
        raise ValueError(_LIQUID_USAGE)
    return _liquid_electorate(session, name).get(entity_id, Decimal(0))


#: model name -> (electorate-finder, weight-finder). Each finder takes the
#: per-proposal scope (the symbol for `share`, the council name for
#: `council`/`weighted`, the polity name for `liquid`, ignored for
#: `citizen`). Add a row to add a form of government; the proposal/vote/
#: enact ops never change.
WEIGHT_MODELS = {
    "citizen": (_citizen_electorate, _citizen_weight),
    "share": (_share_electorate, _share_weight),
    "council": (_council_electorate, _council_weight),
    "weighted": (_weighted_electorate, _weighted_weight),
    "liquid": (_liquid_electorate, _liquid_weight),
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
