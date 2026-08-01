"""Entity capabilities — the privilege model that gates privileged intents.

The engine's ownership invariant says an intent may only move *its own*
assets. Capabilities are the layer above that: they answer "which entities
are allowed to do the things that are *not* pure self-directed action" —
create money, compel a transfer from someone else's account (tax/seizure),
or change policy.

This is the platform's rule layer for actor authorisation
(see `docs/actors.md` Fork 2 and `docs/design.md` §2). The model:

- Every entity carries a set of capability strings (the `capabilities`
  column on `Entity`).
- `Entity.has_capability(name)` is the single check site. The legacy
  `is_monetary_authority` flag is kept as a backward-compatible alias that
  *implies* the monetary capability, so nothing already created stops
  working; new privileged actions use the column directly.
- `INTENT_CAPABILITIES` maps an intent type to the capability required to
  queue it. `scripting.resolve_intent` consults this table before dispatch,
  so a capability denial is a clean rejection at the boundary — the same
  place ownership is enforced — rather than a buried exception.

Capabilities not yet wired to an action (seize)
are declared here as constants and listed for documentation, but absent
from `INTENT_CAPABILITIES` until the action exists (step 4+). Declaring
them now keeps the vocabulary stable across the build.
"""

# --- capability names ------------------------------------------------------

#: Create/destroy base money. Today granted via the legacy
#: `is_monetary_authority` flag; going forward, a capability in its own
#: right.
MONETARY_AUTHORITY = "monetary_authority"

#: Compel a transfer out of an account the entity does not own, under a
#: declared votable rule (tax collection). Wired via `services.levy` and
#: the `levy` intent; generalises `_apply_estate` from death to policy.
LEVY = "levy"

#: Change fiscal policy parameters (tax rates, UBI schedules) stored as
#: the `fiscal_policy` WorldSetting. Wired via `services.set_fiscal_policy`
#: and the `set_fiscal_policy` intent (step 3 — government as policy actor).
SET_FISCAL_POLICY = "set_fiscal_policy"

#: Expropriate assets outright (seizure). Future; shares the levy
#: mechanism under a different rule class.
SEIZE = "seize"

#: Confer capabilities on another entity. Future, meta — the act of
#: granting power must itself be governed (vote / constitutional process),
#: so it is admin-only until that machinery exists.
GRANT_CAPABILITY = "grant_capability"

#: All declared capability names, for validation and introspection.
ALL = frozenset({
    MONETARY_AUTHORITY,
    LEVY,
    SET_FISCAL_POLICY,
    SEIZE,
    GRANT_CAPABILITY,
})

# --- intent → required capability -----------------------------------------

#: The intent types that require a capability, and which one. An intent not
#: listed here requires none — ordinary self-directed action (trade,
#: produce, move your own money) is gated only by ownership. Add rows as
#: the corresponding action is built.
INTENT_CAPABILITIES: dict[str, str] = {
    "issue_money": MONETARY_AUTHORITY,
    "retire_money": MONETARY_AUTHORITY,
    "levy": LEVY,                         # step 2 — compel a transfer under a declared rule
    "set_fiscal_policy": SET_FISCAL_POLICY,  # step 3 — set the votable fiscal-policy dict
    # "seize": SEIZE,                        # future — expropriate goods/parcels
}


def required_for(intent_type: str) -> str | None:
    """The capability required to queue `intent_type`, or None."""
    return INTENT_CAPABILITIES.get(intent_type)
