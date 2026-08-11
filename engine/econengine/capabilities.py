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

Capabilities as policy data are themselves policy the platform drives;
every declared capability now gates at least one intent.
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

#: Enact a new version of a law — retire the active POLICY / BEHAVIOUR /
#: HOOK script of a lineage and activate a new one via the governed
#: lifecycle (`services.set_script`). The writable surface a vote drives
#: (step 4a). Validators are excluded: they are the constitution,
#: amendable only via the constitutional process (4b).
LEGISLATE = "legislate"

#: Amend the constitution — add/amend/retire a VALIDATOR script, or change
#: the voting-system floor (the supermajority threshold/quorum), both
#: through the governed lifecycle (`services.set_validator` /
#: `services.set_constitution`). The exercise of constitutional power;
#: a constitutional proposal's enactment requires it, plus a supermajority
#: (step 4b). Where `legislate` writes ordinary law, this writes the rules
#: ordinary law must obey.
AMEND_CONSTITUTION = "amend_constitution"

#: Expropriate assets outright — goods and/or parcels, not money (the
#: goods/parcels half of enforced state action; levy is the money half).
#: Wired via `services.seize` and the `seize` intent under its own
#: capability, sharing levy's gating model (capability + declared rule +
#: VALIDATOR veto).
SEIZE = "seize"

#: Confer or revoke capabilities on another entity — the meta-privilege of
#: changing *who can exercise power*. Both the `grant_capability` and
#: `revoke_capability` intents gate on this single capability (conferring
#: and withdrawing power are the same meta-act). It is itself governed:
#: checked at the intent boundary AND in the service, vetoable by a
#: VALIDATOR, and (as a proposal mutation) a constitutional-tier op needing
#: supermajority. Free-grant model: a holder may confer any *declared*
#: capability on any entity (a legislature constitutes agencies with powers
#: it does not itself exercise); the safety floor is the capability gate +
#: validator veto + the supermajority, not "you may only delegate what you
#: hold."
GRANT_CAPABILITY = "grant_capability"

#: Bring a new entity into being during a tick -- the one genuinely new
#: mechanism of Step 6c (``docs/actors.md``). ``spawn_entity`` stamps an
#: immutable generic-``parents`` provenance list, sets ``owner_id``
#: (defaulting to the caller's owner), always creates an empty account, and
#: fires VALIDATORs; it does NOT endow (starting wealth is a post-spawn
#: transfer, policy not mechanism). Fail-closed by default: nobody spawns
#: until a world grants this capability. Three concentric gates apply:
#: this capability (constitutional), then server hard caps (engine
#: invariant, non-votable), then world rules (validators, votable).
SPAWN = "spawn"

#: All declared capability names, for validation and introspection.
ALL = frozenset({
    MONETARY_AUTHORITY,
    LEVY,
    SET_FISCAL_POLICY,
    LEGISLATE,
    AMEND_CONSTITUTION,
    SEIZE,
    GRANT_CAPABILITY,
    SPAWN,
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
    "set_script": LEGISLATE,               # step 4a — enact a new version of a (non-validator) law
    "set_validator": AMEND_CONSTITUTION,   # step 4b — amend a VALIDATOR (the constitution)
    "set_constitution": AMEND_CONSTITUTION,  # step 4b — amend the voting-system floor
    "grant_capability": GRANT_CAPABILITY,   # confer a capability (meta; constitutional-tier mutation)
    "revoke_capability": GRANT_CAPABILITY,  # revoke a capability (meta; constitutional-tier mutation)
    "spawn_entity": SPAWN,                  # bring a new entity into being during a tick (Step 6c)
    # NOTE: `enact` is deliberately NOT here. The capability an enactment
    # needs is data on the proposal: ordinary -> legislate, constitutional ->
    # amend_constitution. It is checked in resolve_intent's enact branch
    # after the proposal (and its type) is loaded — the one intent whose
    # required capability is not a pure function of its name.
    "seize": SEIZE,                       # expropriate goods/parcels (the goods half of levy's primitive)
}


def required_for(intent_type: str) -> str | None:
    """The capability required to queue `intent_type`, or None."""
    return INTENT_CAPABILITIES.get(intent_type)
