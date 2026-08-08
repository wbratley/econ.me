"""Delegation registers — the redirect graph for liquid democracy.

Liquid democracy is direct democracy plus a *delegation*: a voter may
redirect their vote to a trusted delegate, who votes it together with
their own (and may themselves delegate onward — transitively). It is the
last weight model (docs/actors.md): the electorate is still "who votes,"
the weight function is still "how much," and the proposal/vote/enact
machinery is untouched. Only the weight function changes — each member's
weight is 1 plus the weight delegated *to* them, resolved transitively.

A delegation register is authored policy data, exactly like a council
register (``councils.py``): a single ``WorldSetting`` row the
``liquid:{name}`` weight model resolves against. The name scopes the
delegation graph — you may delegate your economic-policy vote to one
expert and your foreign-policy vote to another — while the *base*
electorate (the pool of voters) is, as in direct democracy, every active
INDIVIDUAL. With an empty register (or none) ``liquid:{name}`` reduces to
``citizen``: everyone, weight 1.

This module is pure data access — the membership analogue of
``councils.py`` and the constitutional analogue of ``fiscal.py``. It holds
no authority: setting delegations is an admin/platform act today
(``PUT /admin/delegations/{name}``); in a self-governing world delegation
is itself a vote the platform drives. The engine only *reads* it.

Register format: a JSON object ``{delegator_id: delegate_id}``. The weight
model resolves the transitive closure with cycle detection: each member's
unit of weight flows to the terminal voter at the end of their delegation
chain (the first non-delegating member reached). A chain that cycles, or
that points outside the active-individual pool, is *stranded* — its weight
is dropped, a fail-safe that signals a broken graph rather than inflating
anyone. A delegator (someone who redirects) leaves the electorate: they
voted by delegating.
"""

from typing import Mapping

from sqlalchemy.orm import Session

from .models import WorldSetting

#: The world-setting key prefix for delegation registers. The graph for the
#: liquid polity named ``senate`` lives under ``liquid:senate``.
LIQUID_PREFIX = "liquid:"


def _key(name: str) -> str:
    return LIQUID_PREFIX + name


def get_delegations(session: Session, name: str) -> dict[str, str]:
    """The delegation graph ``{delegator_id: delegate_id}`` for polity
    ``name``, or ``{}`` if no register exists (no delegations = direct
    democracy). Returns raw stored strings; the weight model does the
    transitive resolution."""
    setting = session.get(WorldSetting, _key(name))
    if setting is None or not isinstance(setting.value, dict):
        return {}
    return {str(k): str(v) for k, v in setting.value.items()}


def set_delegations(
    session: Session,
    name: str,
    delegations: Mapping[str, object],
) -> WorldSetting:
    """Replace polity ``name``'s delegation graph wholesale.

    ``delegations`` is a ``{delegator_id: delegate_id}`` mapping. Replacing
    wholesale makes delegation changes atomic. Self-delegation
    (``{A: A}``) is rejected — it is a structural no-op whose weight the
    resolver would strand anyway; catching it here gives a clear error. An
    empty mapping is rejected (use ``delete_delegations`` to clear a graph
    back to pure direct democracy).

    No authority check here — this is data access. Seeding is an admin act
    today (``PUT /admin/delegations/{name}``).
    """
    if not isinstance(delegations, Mapping):
        raise ValueError("delegations must be a {delegator_id: delegate_id} map")
    normalised = {str(k): str(v) for k, v in delegations.items()}
    if not normalised:
        raise ValueError("a delegation register must have at least one edge "
                         "(use delete to clear)")
    for delegator, delegate in normalised.items():
        if delegator == delegate:
            raise ValueError(f"self-delegation is not allowed ({delegator})")
    key = _key(name)
    setting = session.get(WorldSetting, key)
    if setting is None:
        setting = WorldSetting(key=key, value=normalised)
        session.add(setting)
    else:
        setting.value = normalised
    session.flush()
    return setting


def delete_delegations(session: Session, name: str) -> bool:
    """Remove polity ``name``'s delegation register (returning to direct
    democracy). Returns whether a row was deleted (False if it never
    existed)."""
    setting = session.get(WorldSetting, _key(name))
    if setting is None:
        return False
    session.delete(setting)
    session.flush()
    return True
