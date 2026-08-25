"""The action registry: readable text for every action (Phase 3b, game.md §15.2).

Every intent type gets a sentence template; outcome events render too —
fills ("sold 2 ORE for 10 COIN at clearing 5"), process lifecycle
("gathered — 3 BERRIES"; branch labels: "hunted — nothing"), need
outcomes, unlocks, spawns, incapacity, estate application. Rejections
are included ("bid 12 for BREAD — refused: insufficient funds"): an
attempt is an action.

The registry is **total by construction**: a fixed renderer covers every
engine outcome type, a template covers every intent type the engine
dispatches, and anything else falls back to a generic render rather than
raising — an unrenderable action is impossible, not a silent gap. The
same rows are the "API docs" manifest: each template knows the params
that matter.

Everything here is a pure function of (event payload, a symbol → name
lookup). Rendered text never enters ``events_hash``, so determinism,
replay, and the RNG commit-reveal chain are untouched. Served by
``GET /entities/{id}/activity``, the world ``GET /activity``, and MCP
``entity_activity`` (§15.3).
"""
from econengine.capabilities import INTENT_CAPABILITIES
from econengine.catalog import _num

#: The fixed outcome-event types the engine emits (grep-verified across
#: goods/needs/markets/production/parcels/conditions/tick/services). Each
#: has a renderer below; the coverage test walks this list.
ENGINE_EVENT_TYPES: tuple[str, ...] = (
    "auction", "auto_issue", "compute_budget_exceeded", "decay",
    "deposit_regen", "entity_incapacitated", "need_satisfied",
    "need_unmet", "order_cancelled", "process_completed",
    "process_failed", "script_error", "script_reverted", "trade",
    "unlocked",
)

#: Intent types resolved by ``scripting.resolve_intent`` that no
#: capability gates (the ungated dispatch chain). The gated half of the
#: vocabulary is ``INTENT_CAPABILITIES``; templates must cover both.
FREE_INTENT_TYPES: tuple[str, ...] = (
    "transfer", "place_order", "cancel_order", "start_process",
    "cancel_process", "transfer_parcel", "set_behaviour", "say",
)


def _g(event: dict, key: str, names=None, default: str = "") -> str:
    """A symbol field rendered with its catalog name when one exists."""
    value = str(event.get(key, default))
    if names:
        return f"{names.get(value, value)}"
    return value


def _intent_text(intent_type: str, params: dict, names=None) -> str:
    p = params or {}
    if intent_type == "transfer":
        return (f"transferred {_num(p.get('amount', 0))} "
                f"{str(p.get('currency', ''))} to {p.get('to_account_id', '?')}")
    if intent_type == "place_order":
        return (f"placed a {p.get('side', '?')} order: "
                f"{_num(p.get('quantity', 0))} {_g(p, 'symbol', names)} "
                f"@ {_num(p.get('limit_price', 0))}")
    if intent_type == "cancel_order":
        return f"cancelled order {p.get('order_id', '?')}"
    if intent_type == "start_process":
        return f"started {p.get('recipe', '?')}"
    if intent_type == "cancel_process":
        return f"cancelled process {p.get('process_id', '?')}"
    if intent_type == "issue_money":
        return (f"issued {_num(p.get('amount', 0))} "
                f"{str(p.get('currency', ''))}")
    if intent_type == "retire_money":
        return (f"retired {_num(p.get('amount', 0))} "
                f"{str(p.get('currency', ''))}")
    if intent_type == "levy":
        return (f"levied {_num(p.get('amount', 0))} "
                f"{str(p.get('currency', ''))} from {p.get('from_account_id', '?')}"
                f" (rule: {p.get('rule_ref', '')})")
    if intent_type == "seize":
        return f"seized goods (rule: {p.get('rule_ref', '')})"
    if intent_type == "spawn_entity":
        return (f"spawned {p.get('entity_type', '?')} "
                f"“{p.get('name', '?')}”")
    if intent_type == "set_behaviour":
        return "submitted a new behaviour script"
    if intent_type == "set_script":
        return f"enacted a new governed script (lineage {p.get('lineage', '?')})"
    if intent_type == "set_validator":
        return "amended the constitution: a validator changed"
    if intent_type == "set_constitution":
        return "amended the constitution"
    if intent_type == "set_fiscal_policy":
        return "set fiscal policy"
    if intent_type == "grant_capability":
        return f"granted capability {p.get('capability', '?')}"
    if intent_type == "revoke_capability":
        return f"revoked capability {p.get('capability', '?')}"
    if intent_type == "create_proposal":
        return "proposed"
    if intent_type == "vote":
        return f"voted {p.get('position', '?')}"
    if intent_type == "enact":
        return f"enacted proposal {p.get('proposal_id', '?')}"
    if intent_type == "transfer_parcel":
        return f"transferred parcel {p.get('parcel_id', '?')}"
    if intent_type == "say":
        return f'says: "{p.get("text", "")}"'
    # Total by construction: an unknown intent still renders.
    detail = ", ".join(f"{k}={v}" for k, v in sorted((p or {}).items()))
    return f"{intent_type} ({detail})" if detail else intent_type


_OUTCOME_RENDERERS = {}


def _renders(event_type: str):
    def register(fn):
        _OUTCOME_RENDERERS[event_type] = fn
        return fn
    return register


@_renders("trade")
def _trade(e, names):
    verb = "bought" if e.get("side") == "buy" else "sold"
    return (f"{verb} {_num(e.get('quantity', 0))} {_g(e, 'market', names)} "
            f"for {_num(e.get('cost', 0))} @ {_num(e.get('price', 0))}")


@_renders("order_cancelled")
def _order_cancelled(e, names):
    return f"order cancelled on {_g(e, 'market', names)}: {e.get('reason', '')}".rstrip(": ")


@_renders("auction")
def _auction(e, names):
    return (f"auction: {_g(e, 'market', names)} cleared "
            f"{_num(e.get('volume', 0))} @ {_num(e.get('price', 0))} "
            f"({e.get('trades', 0)} trades)")


@_renders("process_completed")
def _process_completed(e, names):
    recipe = e.get("recipe", "?")
    if e.get("facility"):
        return f"{recipe.lower().replace('_', ' ')}: built a {e['facility']}"
    outputs = e.get("outputs") or {}
    what = ", ".join(f"+{_num(q)} {_g({'symbol': s}, 'symbol', names)}"
                     for s, q in outputs.items()) or "nothing"
    return f"{recipe.lower().replace('_', ' ')}: {what}"


@_renders("process_failed")
def _process_failed(e, names):
    return (f"{str(e.get('recipe', '?')).lower().replace('_', ' ')} failed: "
            f"{e.get('reason', '?')}")


@_renders("unlocked")
def _unlocked(e, names):
    return f"unlocked {e.get('technology', '?')} ({e.get('scope', '?')}-scoped)"


@_renders("need_satisfied")
def _need_satisfied(e, names):
    # Verb-neutral: "warmth met: ate 1.5" read wrong; the payload carries
    # no need semantics, so the sentence states quantities only.
    return (f"{str(e.get('need', '?')).lower()} met: "
            f"{_num(e.get('consumed', 0))} of {_num(e.get('required', 0))}")


@_renders("need_unmet")
def _need_unmet(e, names):
    base = (f"{str(e.get('need', '?')).lower()} unmet: only "
            f"{_num(e.get('consumed', 0))} of {_num(e.get('required', 0))}")
    if e.get("condition"):
        base += f" — {e['condition']} +{_num(e.get('granted', 0))}"
    return base


@_renders("decay")
def _decay(e, names):
    symbol = _g(e, 'symbol', names)
    if e.get("condition"):
        # Condition goods shed quantity as recovery or relapse (HUNGER
        # easing, WARMTH fading) -- falling is the fact, rotting is not.
        return (f"condition {symbol} fell {_num(e.get('decayed', 0))} "
                f"across {e.get('holders', 0)} holders")
    return (f"decay: {_num(e.get('decayed', 0))} "
            f"{symbol} rotted across "
            f"{e.get('holders', 0)} holders")


@_renders("auto_issue")
def _auto_issue(e, names):
    return (f"issued {_num(e.get('issued', 0))} {_g(e, 'symbol', names)} "
            f"to {e.get('recipients', 0)} entities")


@_renders("deposit_regen")
def _deposit_regen(e, names):
    return (f"seam regrew {_num(e.get('regenerated', 0))} "
            f"{_g(e, 'symbol', names)} (now {_num(e.get('quantity', 0))})")


@_renders("entity_incapacitated")
def _incapacitated(e, names):
    line = (f"incapacitated: {e.get('condition', '?')} reached "
            f"{_num(e.get('quantity', 0))} (threshold "
            f"{_num(e.get('threshold', 0))})")
    if e.get("recipient_id"):
        line += "; estate applied"
    return line


@_renders("script_error")
def _script_error(e, names):
    return f"behaviour crashed: {e.get('error', '?')}"


@_renders("script_reverted")
def _script_reverted(e, names):
    return "behaviour reverted to its last working version after repeated crashes"


@_renders("compute_budget_exceeded")
def _budget(e, names):
    return "compute budget exceeded — script skipped this tick"


def render_event(event: dict, names: dict[str, str] | None = None) -> str:
    """One event → one readable sentence. Total: never raises.

    ``names`` is the symbol → display-name lookup from the catalog (the
    join §15.3 asks for); symbols render bare when it is absent.
    """
    etype = str(event.get("type", "unknown"))
    renderer = _OUTCOME_RENDERERS.get(etype)
    if renderer is not None:
        return renderer(event, names)
    if "params" in event or etype in INTENT_CAPABILITIES or etype in FREE_INTENT_TYPES:
        text = _intent_text(etype, event.get("params") or {}, names)
        status = event.get("status")
        if status == "rejected":
            text += f" — refused: {event.get('reason', '?')}"
        elif status == "applied":
            pass  # the sentence IS the action; success is the unmarked case
        return text
    # A world fact of a shape this build never met: render, don't raise.
    return etype.replace("_", " ")


def symbol_names(catalog: dict) -> dict[str, str]:
    """The catalog join: symbol → display name, dropping empty names."""
    out = {}
    for good in catalog.get("goods", []):
        if good.get("name"):
            out[good["symbol"]] = good["name"]
    return out
