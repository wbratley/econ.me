"""The seize mechanism — expropriation of goods and parcels (the
goods/parcels half of enforced state action; levy is the money half).

Seize is the privilege layer above ownership made concrete for non-money
assets: an entity holding the ``seize`` capability may compel movement of
goods and/or parcels out of an entity it does NOT own, into a declared
recipient (itself by default), under a declared ``rule_ref``. It
generalises the estate sweep (``conditions._apply_estate``) — which already
moves a dead entity's goods and parcels by engine authority — from death
to enacted policy.

All the safety lives in the gating, not the movement:
  - capability (``seize``) checked at the intent boundary AND in the service;
  - the recipient defaults to the authority; a different recipient is the
    redistribution case, gated by a VALIDATOR veto;
  - goods are goods-conserving (debit victim / credit recipient, raises if
    the victim is short — fail-closed);
  - parcels are reassigned only if the victim owns them and no running
    process is bound to them;
  - a VALIDATOR may veto an illegal seizure under ``rule_ref`` (fail-closed).
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, markets, parcels
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType, Script, ScriptType
from econengine.scripting import OperationVetoedError, resolve_intent
from econengine.services import (
    MissingCapabilityError,
    create_entity,
    seize,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A victim holding goods (1000 GRAIN) and a parcel of land; a
    seize-capable government; and a plain government with no capabilities.
    A third individual (the redistribution target) holds nothing."""
    victim = create_entity(session, "Victim", EntityType.INDIVIDUAL)
    gov = create_entity(session, "State", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.SEIZE]
    plain = create_entity(session, "PlainGov", EntityType.GOVERNMENT)  # no caps
    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    markets.adjust_holding(session, victim, "GRAIN", Decimal("1000"))
    plot = parcels.create_parcel(session, "FIELD", name="North 40", owner=victim)
    session.flush()
    return {"victim": victim, "gov": gov, "plain": plain, "heir": heir,
            "plot": plot}


def make_script(session, name, source, script_type, entity=None, **kwargs):
    script = Script(
        name=name, source=source, script_type=script_type,
        entity_id=entity.id if entity else None, **kwargs,
    )
    session.add(script)
    session.flush()
    return script


def holding_qty(session, entity, symbol):
    h = markets.get_holding(session, entity.id, symbol)
    return h.quantity if h else Decimal("0")


# ---------------------------------------------------------------------------
# services.seize — direct (in-process) callers: GOODS
# ---------------------------------------------------------------------------

def test_seize_moves_goods_from_victim_to_authority(world, session):
    """The core: goods leave the victim's holding and enter the authority's."""
    seize(session, world["gov"], world["victim"],
          symbol="GRAIN", quantity=Decimal("300"), rule_ref="tax:inkind")
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("700")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("300")


def test_seize_goods_is_goods_conserving(world, session):
    """Nothing is created or destroyed — the victim's loss is the authority's
    gain (unlike money issuance, which expands supply)."""
    before = (holding_qty(session, world["victim"], "GRAIN")
              + holding_qty(session, world["gov"], "GRAIN"))
    seize(session, world["gov"], world["victim"],
          symbol="GRAIN", quantity=Decimal("250"), rule_ref="confiscation")
    after = (holding_qty(session, world["victim"], "GRAIN")
             + holding_qty(session, world["gov"], "GRAIN"))
    assert before == after == Decimal("1000")


def test_seize_symbol_is_case_insensitive(world, session):
    """Symbols are stored upper-cased (adjust_holding); the scope matches
    either way, so seize matches the estate sweep's convention."""
    seize(session, world["gov"], world["victim"],
          symbol="grain", quantity=Decimal("10"), rule_ref="r")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("10")


def test_seize_defaults_recipient_to_authority(world, session):
    """With no to_entity the goods land in the authority's own holding — the
    common expropriation case (the state seizes into its own stores)."""
    result = seize(session, world["gov"], world["victim"],
                   symbol="GRAIN", quantity=Decimal("100"), rule_ref="r")
    assert result["to_entity_id"] == world["gov"].id
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("100")


def test_seize_redirects_to_declared_recipient(world, session):
    """A different to_entity is the redistribution case: the authority is the
    actor, the recipient is declared. This is what the levy doc means by
    "redirecting between two third parties ... seizure under a different
    rule." Goods bypass the victim AND land on a third party."""
    seize(session, world["gov"], world["victim"],
          symbol="GRAIN", quantity=Decimal("400"), to_entity=world["heir"],
          rule_ref="redistribution:land_reform")
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("600")
    assert holding_qty(session, world["heir"], "GRAIN") == Decimal("400")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("0")


def test_seize_rejects_insufficient_holdings_without_mutating(world, session):
    """Fail-closed: a victim short of the seized quantity raises and leaves
    both holdings untouched (the debit is attempted first)."""
    with pytest.raises(markets.InsufficientHoldingsError):
        seize(session, world["gov"], world["victim"],
              symbol="GRAIN", quantity=Decimal("5000"), rule_ref="r")
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("1000")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("0")


def test_seize_rejects_non_positive_quantity(world, session):
    with pytest.raises(ValueError, match="positive"):
        seize(session, world["gov"], world["victim"],
              symbol="GRAIN", quantity=Decimal("0"), rule_ref="r")
    with pytest.raises(ValueError, match="positive"):
        seize(session, world["gov"], world["victim"],
              symbol="GRAIN", quantity=Decimal("-5"), rule_ref="r")


def test_seize_rejects_half_goods_spec(world, session):
    """A goods seizure needs both symbol and quantity."""
    with pytest.raises(ValueError, match="both symbol and quantity"):
        seize(session, world["gov"], world["victim"],
              symbol="GRAIN", rule_ref="r")            # quantity missing
    with pytest.raises(ValueError, match="both symbol and quantity"):
        seize(session, world["gov"], world["victim"],
              quantity=Decimal("5"), rule_ref="r")     # symbol missing


def test_seize_rejects_empty_spec(world, session):
    """At least one of goods or parcels must be named."""
    with pytest.raises(ValueError, match="goods .* or parcels"):
        seize(session, world["gov"], world["victim"], rule_ref="r")


# ---------------------------------------------------------------------------
# services.seize — PARCELS
# ---------------------------------------------------------------------------

def test_seize_moves_parcel_to_authority(world, session):
    """A parcel is reassigned from the victim to the authority — eminent
    domain. Ownership is the only thing that moves (no transaction)."""
    result = seize(session, world["gov"], world["victim"],
                   parcel_ids=[world["plot"].id], rule_ref="eminent_domain:north40")
    assert result["parcels"] == 1
    session.refresh(world["plot"])
    assert world["plot"].owner_id == world["gov"].id


def test_seize_redirects_parcel_to_third_party(world, session):
    """Parcels may also be redirected — the land-reform case."""
    seize(session, world["gov"], world["victim"],
          parcel_ids=[world["plot"].id], to_entity=world["heir"],
          rule_ref="redistribution")
    session.refresh(world["plot"])
    assert world["plot"].owner_id == world["heir"].id


def test_seize_rejects_parcel_the_victim_does_not_own(world, session):
    """The victim must currently own the parcel — the authority cannot seize
    a parcel from someone who does not hold it."""
    stranger = create_entity(session, "Stranger", EntityType.INDIVIDUAL)
    other = parcels.create_parcel(session, "FIELD", name="South 40", owner=stranger)
    session.flush()
    with pytest.raises(ValueError, match="does not own parcel"):
        seize(session, world["gov"], world["victim"],
              parcel_ids=[other.id], rule_ref="r")
    session.refresh(other)
    assert other.owner_id == stranger.id      # unchanged


def test_seize_rejects_parcel_with_running_process(world, session):
    """A parcel with a running production process bound to it cannot be
    seized (the ownership flip refuses it), matching transfer_parcel."""
    from econengine import production
    from econengine.models import Recipe
    # a 2-tick recipe bound to the plot; start_process leaves it RUNNING
    session.add(Recipe(code="GROW", name="grow", duration_ticks=2))
    session.flush()
    production.start_process(session, world["victim"], "GROW",
                             parcel_id=world["plot"].id)
    with pytest.raises(ValueError, match="running processes"):
        seize(session, world["gov"], world["victim"],
              parcel_ids=[world["plot"].id], rule_ref="r")
    session.refresh(world["plot"])
    assert world["plot"].owner_id == world["victim"].id   # unchanged


def test_seize_goods_and_parcels_together(world, session):
    """A single seizure may take goods AND parcels in one atomic act (seize
    a farm and its standing crop together)."""
    result = seize(session, world["gov"], world["victim"],
                   symbol="GRAIN", quantity=Decimal("500"),
                   parcel_ids=[world["plot"].id], rule_ref="seizure:farm")
    assert result["parcels"] == 1
    assert result["goods_quantity"] == "500"
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("500")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("500")
    session.refresh(world["plot"])
    assert world["plot"].owner_id == world["gov"].id


# ---------------------------------------------------------------------------
# capability gating
# ---------------------------------------------------------------------------

def test_seize_rejects_authority_without_capability(world, session):
    with pytest.raises(MissingCapabilityError) as exc_info:
        seize(session, world["plain"], world["victim"],
              symbol="GRAIN", quantity=Decimal("1"), rule_ref="r")
    assert exc_info.value.capability == capabilities.SEIZE
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("1000")


def test_missing_capability_error_is_value_error(session):
    """MissingCapabilityError subclasses ValueError so resolve_intent's
    blanket ValueError handler turns it into a clean rejection."""
    assert issubclass(MissingCapabilityError, ValueError)


# ---------------------------------------------------------------------------
# resolve_intent — the intent surface
# ---------------------------------------------------------------------------

def _seize_intent(entity_id, from_id, *, symbol="300", quantity="300",
                  parcel_ids=None, to_id=None, rule_ref="tax:inkind"):
    params = {"from_entity_id": from_id, "rule_ref": rule_ref}
    if symbol is not None:
        params["symbol"] = symbol
    if quantity is not None:
        params["quantity"] = quantity
    if parcel_ids is not None:
        params["parcel_ids"] = json.dumps(parcel_ids)
    if to_id is not None:
        params["to_entity_id"] = to_id
    resource_ids = [from_id]
    if to_id is not None:
        resource_ids.append(to_id)
    return Intent(entity_id=entity_id, intent_type="seize", params=params,
                  resource_ids=resource_ids)


def test_intent_seize_bypasses_ownership_of_source(world, session):
    """Through the intent surface the authority seizes goods it does NOT own
    — the whole point. The victim's holding falls, the authority's rises."""
    out = resolve_intent(session, _seize_intent(
        world["gov"].id, world["victim"].id, symbol="GRAIN", quantity="300"))
    assert out["status"] == "applied"
    assert out["seized_goods"] == "300"
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("700")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("300")


def test_intent_seize_redirects_to_recipient(world, session):
    out = resolve_intent(session, _seize_intent(
        world["gov"].id, world["victim"].id, symbol="GRAIN", quantity="200",
        to_id=world["heir"].id))
    assert out["status"] == "applied"
    assert holding_qty(session, world["heir"], "GRAIN") == Decimal("200")


def test_intent_seize_moves_parcels(world, session):
    out = resolve_intent(session, _seize_intent(
        world["gov"].id, world["victim"].id, symbol=None, quantity=None,
        parcel_ids=[world["plot"].id], rule_ref="eminent_domain"))
    assert out["status"] == "applied"
    assert out["seized_parcels"] == 1
    session.refresh(world["plot"])
    assert world["plot"].owner_id == world["gov"].id


def test_intent_seize_rejects_entity_without_capability(world, session):
    out = resolve_intent(session, _seize_intent(
        world["plain"].id, world["victim"].id, symbol="GRAIN", quantity="50"))
    assert out["status"] == "rejected"
    assert capabilities.SEIZE in out["reason"]
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("1000")


def test_intent_seize_rejects_insufficient_holdings(world, session):
    out = resolve_intent(session, _seize_intent(
        world["gov"].id, world["victim"].id, symbol="GRAIN", quantity="99999"))
    assert out["status"] == "rejected"
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("1000")


def test_intent_seize_rejects_unknown_source_entity(world, session):
    out = resolve_intent(session, _seize_intent(
        world["gov"].id, "no-such-entity", symbol="GRAIN", quantity="1"))
    assert out["status"] == "rejected"
    assert "source entity" in out["reason"]


# ---------------------------------------------------------------------------
# VALIDATOR — the constitutional backstop on seizure
# ---------------------------------------------------------------------------

SEIZE_CAP_200 = """
if ctx.op.type == 'seize' and ctx.op.quantity ~= nil
   and tonumber(ctx.op.quantity) > 200 then
    return {allow=false, reason="seizure exceeds statutory cap"}
end
"""


def test_validator_vetoes_excessive_seizure(world, session):
    """A VALIDATOR may cap a seizure under its declared rule — the safety
    valve against an over-reaching authority. Fail-closed: nothing moves."""
    make_script(session, "cap", SEIZE_CAP_200, ScriptType.VALIDATOR)
    with pytest.raises(OperationVetoedError, match="statutory cap"):
        seize(session, world["gov"], world["victim"],
              symbol="GRAIN", quantity=Decimal("500"), rule_ref="r")
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("1000")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("0")


def test_validator_allows_seizure_under_cap(world, session):
    make_script(session, "cap", SEIZE_CAP_200, ScriptType.VALIDATOR)
    seize(session, world["gov"], world["victim"],
          symbol="GRAIN", quantity=Decimal("150"), rule_ref="r")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("150")


def test_vetoed_seize_intent_is_rejected_not_raised(world, session):
    """Through the intent surface a veto becomes a clean rejection, not a
    raised exception (the resolver swallows ValueError subclasses)."""
    make_script(session, "cap", SEIZE_CAP_200, ScriptType.VALIDATOR)
    out = resolve_intent(session, _seize_intent(
        world["gov"].id, world["victim"].id, symbol="GRAIN", quantity="500"))
    assert out["status"] == "rejected"
    assert "statutory cap" in out["reason"]


# ---------------------------------------------------------------------------
# ctx.action.seize — reachable from the script layer (the policy driver)
# ---------------------------------------------------------------------------

def test_policy_script_can_seize_each_tick(world, session):
    """A seize-capable government's POLICY script fires ctx.action.seize each
    tick: the capability gate admits it and goods move from the victim into
    the state. This is the goods analogue of step 3's levy driver."""
    make_script(
        session, "in-kind-tax",
        f"ctx.action.seize('{world['victim'].id}', "
        f"{{symbol='GRAIN', quantity='100'}}, 'tax:inkind')",
        ScriptType.POLICY, entity=world["gov"],
    )
    from econengine.tick import run_tick
    run_tick(session)
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("900")
    assert holding_qty(session, world["gov"], "GRAIN") == Decimal("100")


def test_policy_script_can_seize_parcels_via_action(world, session):
    """The script action also drives parcel seizure (eminent domain per tick)."""
    make_script(
        session, "land-reform",
        f"ctx.action.seize('{world['victim'].id}', "
        f"{{parcel_ids={{'{world['plot'].id}'}}}}, 'eminent_domain')",
        ScriptType.POLICY, entity=world["gov"],
    )
    from econengine.tick import run_tick
    run_tick(session)
    session.refresh(world["plot"])
    assert world["plot"].owner_id == world["gov"].id


def test_policy_script_without_capability_seize_is_rejected(world, session):
    """A government without the seize capability cannot expropriate, even from
    a script — the gate holds at the intent boundary."""
    make_script(
        session, "rogue-seize",
        f"ctx.action.seize('{world['victim'].id}', "
        f"{{symbol='GRAIN', quantity='100'}}, 'r')",
        ScriptType.POLICY, entity=world["plain"],    # no seize capability
    )
    from econengine.tick import run_tick
    run_tick(session)
    assert holding_qty(session, world["victim"], "GRAIN") == Decimal("1000")  # untouched
