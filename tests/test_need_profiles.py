"""Need profiles + holding-rate modifiers (run 56, the run-55 lesson).

Run 55 died of a type-scoped need meeting a creature the type never
meant: COMFORT applied to every INDIVIDUAL, wolves included, and the
squalor ration starved the food web. These tests pin the two-part cure:

- profiles: a creature may omit a need outright or scale its draw --
  stamped from the spawn template at birth, resolved by the pass;
- modifiers: a need's draw may scale by what the body holds (CON makes
  a cheaper hunger), floored at zero, product across symbols.

The default body is exactly the pre-run-56 world: NULL profile draws
every need of the type at its declared rate.
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import needs as needs_mod
from econengine import spawns
from econengine.models import Base, Entity, EntityType, Holding
from econengine.services import create_entity


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_entity(session: Session, name: str, profile: dict | None = None) -> Entity:
    entity = create_entity(session, name, EntityType.INDIVIDUAL)
    if profile is not None:
        entity.need_profile = needs_mod.normalize_need_profile(profile)
    session.flush()
    return entity


def _holding(session: Session, entity: Entity, symbol: str, qty: str) -> None:
    session.add(Holding(entity_id=entity.id, symbol=symbol, quantity=Decimal(qty)))
    session.flush()


# --- profiles ------------------------------------------------------------


def test_the_default_body_is_the_old_world(session):
    """NULL profile: every need of the type applies at its declared rate --
    the exact pre-run-56 behavior, so every existing world keeps its
    draws on upgrade."""
    needs_mod.create_need(
        session, "COMFORT", Decimal("0.1"), ["COMFORT"],
        condition_symbol="SQUALOR", condition_quantity=Decimal("0.02"))
    entity = _make_entity(session, "House")
    _holding(session, entity, "COMFORT", "5")

    events = needs_mod.run_consumption(session, 1)
    comfort = next(e for e in events if e["need"] == "COMFORT")
    assert comfort["consumed"] == "0.1000"
    assert comfort["required"] == "0.1000"


def test_an_omitted_need_draws_nothing_and_reports_nothing(session):
    """The wolf does not want comfort: omitted from the profile, the need
    draws nothing, emits no event, and credits no condition -- the
    run-55 kill chain, cut at its root."""
    needs_mod.create_need(
        session, "COMFORT", Decimal("0.1"), ["COMFORT"],
        condition_symbol="SQUALOR", condition_quantity=Decimal("0.02"))
    wolf = _make_entity(session, "Wolf", {"omit": ["COMFORT"]})
    house = _make_entity(session, "House")
    for e in (wolf, house):
        _holding(session, e, "COMFORT", "0")

    events = needs_mod.run_consumption(session, 1)
    ids = [e["entity_id"] for e in events if e["need"] == "COMFORT"]
    assert wolf.id not in ids
    assert house.id in ids
    # no squalor accrues to the wolf for a want it does not have
    squalor = session.query(Holding).filter_by(
        entity_id=wolf.id, symbol="SQUALOR").one_or_none()
    assert squalor is None


def test_a_scaled_draw_shows_in_the_event(session):
    """rates scale the draw, and the event's `required` carries the
    creature's number -- a script reading its own events sees its own
    metabolism, not the world's."""
    needs_mod.create_need(session, "FOOD", Decimal("0.5"), ["SATIETY"])
    boar = _make_entity(session, "Boar", {"rates": {"FOOD": "0.5"}})
    _holding(session, boar, "SATIETY", "10")

    events = needs_mod.run_consumption(session, 1)
    food = next(e for e in events if e["need"] == "FOOD")
    assert food["required"] == "0.2500"
    assert food["consumed"] == "0.2500"


def test_an_unmentioned_need_applies_at_full_rate(session):
    """Profiles are blacklists-with-scales, never exhaustive lists: a
    need the profile does not mention (including one authored later)
    applies to old creatures by default."""
    needs_mod.create_need(session, "FOOD", Decimal("0.5"), ["SATIETY"])
    wolf = _make_entity(session, "Wolf", {"omit": ["COMFORT"]})
    _holding(session, wolf, "SATIETY", "10")

    events = needs_mod.run_consumption(session, 1)
    assert any(e["entity_id"] == wolf.id and e["need"] == "FOOD" for e in events)


def test_profile_normalization_is_strict(session):
    """The template is data and never trusted: codes normalize, empty
    shapes collapse to the default body, and a rate outside (0, 10] is
    refused -- abolition is what omit is for."""
    assert needs_mod.normalize_need_profile(None) is None
    assert needs_mod.normalize_need_profile({}) is None
    assert needs_mod.normalize_need_profile({"omit": []}) is None
    assert needs_mod.normalize_need_profile(
        {"omit": ["comfort"]}) == {"omit": ["COMFORT"]}
    assert needs_mod.normalize_need_profile(
        {"rates": {"food": 2}}) == {"rates": {"FOOD": "2.0000"}}
    with pytest.raises(ValueError, match="omit the need"):
        needs_mod.normalize_need_profile({"rates": {"FOOD": 0}})
    with pytest.raises(ValueError, match="in \\(0, 10\\]"):
        needs_mod.normalize_need_profile({"rates": {"FOOD": 11}})


# --- holding-rate modifiers ----------------------------------------------


def test_the_body_scales_the_draw(session):
    """CON makes a cheaper hunger: each point multiplies the rate by
    (1 + delta*qty), and the event's required shows the scaled number."""
    needs_mod.create_need(
        session, "FOOD", Decimal("0.5"), ["SATIETY"],
        holding_rate_modifiers={"CON": "-0.08"})
    hardy = _make_entity(session, "Hardy")
    frail = _make_entity(session, "Frail")
    _holding(session, hardy, "CON", "2")
    for e in (hardy, frail):
        _holding(session, e, "SATIETY", "10")

    events = needs_mod.run_consumption(session, 1)
    by_id = {e["entity_id"]: e for e in events}
    assert by_id[frail.id]["required"] == "0.5000"       # no CON: base draw
    assert by_id[hardy.id]["required"] == "0.4200"       # 0.5 * (1 - 0.16)


def test_the_floor_is_zero_not_negative(session):
    """A body cannot draw negative: past the point where the modifiers
    zero the draw, the pass skips the entity entirely -- nothing
    consumed, nothing reported, no division by a zero requirement."""
    needs_mod.create_need(
        session, "FOOD", Decimal("0.5"), ["SATIETY"],
        holding_rate_modifiers={"CON": "-0.2"})
    monk = _make_entity(session, "Monk")
    _holding(session, monk, "CON", "6")                  # 1 - 1.2 < 0

    events = needs_mod.run_consumption(session, 1)
    assert not any(e["entity_id"] == monk.id for e in events)


def test_modifiers_multiply_across_symbols(session):
    """The body's voice is a product across symbols, resolved
    symbol-ascending (determinism invariant)."""
    needs_mod.create_need(
        session, "REST", Decimal("0.25"), ["REST"],
        holding_rate_modifiers={"CON": "-0.10", "AGE": "0.50"})
    elder = _make_entity(session, "Elder")
    _holding(session, elder, "CON", "1")
    _holding(session, elder, "AGE", "2")
    _holding(session, elder, "REST", "10")

    events = needs_mod.run_consumption(session, 1)
    rest = next(e for e in events)
    # 0.25 * (1 - 0.10*1) * (1 + 0.50*2) = 0.25 * 0.9 * 2 = 0.45
    assert rest["required"] == "0.4500"


def test_modifier_map_is_normalized_on_create(session):
    """create_need stores exact-decimal strings symbol-ascending and
    refuses zero deltas (a no-op modifier is a mistake, not a choice)."""
    need = needs_mod.create_need(
        session, "WATER", Decimal("0.25"), ["WATER"],
        holding_rate_modifiers={"age": "0.1", "CON": "-0.05"})
    assert need.holding_rate_modifiers == {"AGE": "0.1000", "CON": "-0.0500"}
    with pytest.raises(ValueError, match="nonzero"):
        needs_mod.create_need(
            session, "AIR", Decimal("0.1"), ["AIR"],
            holding_rate_modifiers={"CON": 0})


# --- the spawn template stamps the biology --------------------------------


def test_spawn_templates_carry_need_profiles(session):
    """template["needs"] lands normalized on the creature at birth -- the
    pack authors the wolf's biology as data, the engine stamps it."""
    spawns.set_script_source(session, "wolf", "-- wolf behaviour")
    spawns.set_rules(session, {
        "from_round": 1, "every_rounds": 1, "up_to": 1, "max_alive": 5,
        "name_prefix": "Wolf Pack",
        "template": {
            "entity_type": "individual",
            "holdings": {"MEAT": 1},
            "script_setting": "wolf",
            "needs": {"omit": ["COMFORT"], "rates": {"FOOD": "0.8"}},
        }})

    born = spawns.apply_on_round(session, 1)

    assert len(born) == 1
    wolf = session.get(Entity, born[0]["entity_id"])
    assert wolf.need_profile == {
        "omit": ["COMFORT"], "rates": {"FOOD": "0.8000"}}


def test_spawn_templates_without_needs_stay_default_bodies(session):
    """A template that mentions no needs spawns a plain creature --
    NULL profile, the pre-run-56 default, so old packs keep running."""
    spawns.set_rules(session, {
        "from_round": 1, "every_rounds": 1, "up_to": 1, "max_alive": 5,
        "name_prefix": "Boar",
        "template": {"entity_type": "individual", "holdings": {"MEAT": 1}}})

    born = spawns.apply_on_round(session, 1)

    boar = session.get(Entity, born[0]["entity_id"])
    assert boar.need_profile is None


# --- the season ladder still resolves under a profile ---------------------


def test_profile_scales_the_season_night_too(session):
    """The specificity ladder (season-night > season > night > base)
    resolves first; the creature's slice scales whatever it resolves to
    -- one definition of the draw, modulated at the end."""
    needs_mod.create_need(
        session, "WARMTH", Decimal("1"), ["WARMTH"],
        night_quantity_per_tick=Decimal("3"),
        season_night_quantity_per_tick={"WINTER": Decimal("4")})
    wolf = _make_entity(session, "Wolf", {"rates": {"WARMTH": "0.5"}})
    _holding(session, wolf, "WARMTH", "10")

    winter_night_tick = 19 * 24 + 21        # day 19, hour ~21: night
    events = needs_mod.run_consumption(session, winter_night_tick)
    warmth = next(e for e in events)
    assert warmth["required"] == "2.0000"   # 4 (winter night) * 0.5
