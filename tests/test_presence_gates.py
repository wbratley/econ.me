"""Presence gates — where you stand gates what you may do (S2).

docs/spatial.md §6: recipes may require the entity's current place to
match (a kind — "any HEARTH" — or an exact key), and a market may sit
at a place, trading only for entities standing there. The checks live
in start_process's requirement pass and place_order; refusals are the
ordinary ValueErrors → rejected results, reasons carrying place names
so a blind script can correct in one tick.

The legacy invariant is the point of Fork 6: gates fire only on
declared data. A recipe or market with NULL gate columns behaves
exactly as before — unplaced entities, mapless worlds, everything.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import catalog, markets, places, production, services
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType
from econengine.scripting import resolve_intent

_PACK = "demo-world"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _map(session):
    """Two hearths (kind matches either), one named river, one post."""
    places.create_place(session, "HEARTH", kind="HEARTH", name="Hearth clearing",
                        pack_id=_PACK)
    places.create_place(session, "HALL", kind="HEARTH", name="The great hall",
                        pack_id=_PACK)
    places.create_place(session, "RIVER", kind="RIVER", name="The River",
                        pack_id=_PACK)
    places.create_place(session, "POST", kind="POST", name="Trading post",
                        pack_id=_PACK)


def _entity(session, name="Wanderer"):
    return services.create_entity(session, name, EntityType.INDIVIDUAL)


def _gated_recipe(session, code="PRAY_AT_HEARTH", **gate):
    return production.create_recipe(
        session, code, inputs={}, outputs={"STONE": Decimal("1")},
        duration_ticks=1, **gate,
    )


# --- recipe gates ----------------------------------------------------------


def test_kind_gate_admits_any_place_of_the_kind(session):
    _map(session)
    _gated_recipe(session, requires_place_kind="hearth")  # lowercased arg
    entity = _entity(session)

    places.move_entity(session, entity, "HALL")  # the OTHER hearth
    process = production.start_process(session, entity, "PRAY_AT_HEARTH")
    assert process.status.value == "running"


def test_kind_gate_refuses_elsewhere_naming_where_you_are(session):
    _map(session)
    _gated_recipe(session, requires_place_kind="HEARTH")
    entity = _entity(session)
    places.move_entity(session, entity, "RIVER")

    with pytest.raises(ValueError, match="must be at a HEARTH"):
        production.start_process(session, entity, "PRAY_AT_HEARTH")
    # the reason names where the entity actually stands (run-15 lesson)
    with pytest.raises(ValueError, match="The River is not one"):
        production.start_process(session, entity, "PRAY_AT_HEARTH")


def test_kind_gate_refuses_the_unplaced(session):
    _map(session)
    _gated_recipe(session, requires_place_kind="HEARTH")
    entity = _entity(session)  # never placed

    with pytest.raises(ValueError, match="you are not on the map"):
        production.start_process(session, entity, "PRAY_AT_HEARTH")


def test_key_gate_pins_the_exact_place(session):
    _map(session)
    _gated_recipe(session, code="FISH_THE_RIVER", requires_place_key="river")
    entity = _entity(session)

    with pytest.raises(ValueError, match="must be at The River"):
        production.start_process(session, entity, "FISH_THE_RIVER")

    places.move_entity(session, entity, "RIVER")
    process = production.start_process(session, entity, "FISH_THE_RIVER")
    assert process.recipe.requires_place_key == "RIVER"  # stored uppercased


def test_key_gate_takes_precedence_over_kind(session):
    # Both declared: the key is the pin; the kind line is the coarser
    # gate it makes redundant. Standing at the key passes even though
    # the kind differs.
    _map(session)
    _gated_recipe(session, code="STRANGE_RITE",
                  requires_place_kind="HEARTH", requires_place_key="RIVER")
    entity = _entity(session)
    places.move_entity(session, entity, "RIVER")

    production.start_process(session, entity, "STRANGE_RITE")  # no raise


def test_gate_on_an_uninstalled_place_is_loud(session):
    _map(session)
    _gated_recipe(session, code="MOON_RITE", requires_place_key="MOON")
    entity = _entity(session)
    places.move_entity(session, entity, "HEARTH")

    with pytest.raises(ValueError, match="no such place is installed"):
        production.start_process(session, entity, "MOON_RITE")


def test_ungated_recipe_needs_no_map(session):
    # Fork 6: the legacy citizen. NULL gates on a world WITH places, and
    # an entity standing nowhere on it — starts exactly as before.
    _map(session)
    _gated_recipe(session, code="PLAIN_WORK")  # no gate kwargs
    entity = _entity(session)

    production.start_process(session, entity, "PLAIN_WORK")  # no raise


# --- market seats ----------------------------------------------------------


def test_placed_market_refuses_the_elsewhere(session):
    _map(session)
    markets.create_market(session, "FISH", "COIN", place="POST")
    entity = _entity(session)
    account = services.create_account(session, entity, "COIN")
    places.move_entity(session, entity, "HEARTH")

    with pytest.raises(ValueError, match="market FISH trades at Trading post"):
        markets.place_order(session, entity.id, "FISH", "buy",
                            Decimal("1"), Decimal("1"), account.id)
    with pytest.raises(ValueError, match="you are at Hearth clearing"):
        markets.place_order(session, entity.id, "FISH", "buy",
                            Decimal("1"), Decimal("1"), account.id)

    places.move_entity(session, entity, "POST")
    order = markets.place_order(session, entity.id, "FISH", "buy",
                                Decimal("1"), Decimal("1"), account.id)
    assert order.status.value == "open"


def test_placed_market_refuses_the_unplaced(session):
    _map(session)
    markets.create_market(session, "FISH", "COIN", place="POST")
    entity = _entity(session)
    account = services.create_account(session, entity, "COIN")

    with pytest.raises(ValueError, match="you are not on the map"):
        markets.place_order(session, entity.id, "FISH", "buy",
                            Decimal("1"), Decimal("1"), account.id)


def test_global_market_trades_with_anyone_anywhere(session):
    # NULL seat = today's market: an unplaced entity on a mapped world
    # trades freely (Fork 5's default).
    _map(session)
    markets.create_market(session, "FISH", "COIN")
    entity = _entity(session)  # unplaced
    account = services.create_account(session, entity, "COIN")

    order = markets.place_order(session, entity.id, "FISH", "buy",
                                Decimal("1"), Decimal("1"), account.id)
    assert order.status.value == "open"


def test_create_market_by_place_object_and_unknown_key(session):
    _map(session)
    post = places.get_place(session, "POST")
    markets.create_market(session, "FISH", "COIN", place=post)
    with pytest.raises(ValueError, match="unknown place 'MOON'"):
        markets.create_market(session, "MOONDUST", "COIN", place="MOON")


# --- refusals as ordinary rejections (the intent surface) -------------------


def test_start_process_gate_is_an_ordinary_rejection(session):
    _map(session)
    _gated_recipe(session, requires_place_kind="HEARTH")
    entity = _entity(session)
    places.move_entity(session, entity, "RIVER")

    outcome = resolve_intent(session, Intent(
        entity_id=entity.id, intent_type="start_process",
        params={"recipe": "PRAY_AT_HEARTH"}, resource_ids=[],
    ))
    assert outcome["status"] == "rejected"
    assert "must be at a HEARTH" in outcome["reason"]
    assert "The River" in outcome["reason"]


def test_place_order_seat_is_an_ordinary_rejection(session):
    _map(session)
    markets.create_market(session, "FISH", "COIN", place="POST")
    entity = _entity(session)
    account = services.create_account(session, entity, "COIN")
    places.move_entity(session, entity, "HEARTH")

    outcome = resolve_intent(session, Intent(
        entity_id=entity.id, intent_type="place_order",
        params={"symbol": "FISH", "side": "buy", "quantity": "1",
                "limit_price": "1", "account_id": account.id},
        resource_ids=[account.id],
    ))
    assert outcome["status"] == "rejected"
    assert "trades at Trading post" in outcome["reason"]


# --- the readable catalog ---------------------------------------------------


def test_catalog_renders_the_requirement_lines(session):
    _map(session)
    kind = _gated_recipe(session, requires_place_kind="HEARTH")
    key = _gated_recipe(session, code="FISH_THE_RIVER",
                        requires_place_key="RIVER")

    effects_kind = catalog.recipe_effects(kind)
    assert "requires presence at a HEARTH" in effects_kind

    effects_key = catalog.recipe_effects(key)
    assert "requires presence at RIVER" in effects_key
    # key declared: the kind line is not doubled in
    assert not any("HEARTH" in line for line in effects_key)


def test_catalog_names_the_market_seat(session):
    _map(session)
    markets.create_market(session, "FISH", "COIN", place="POST")
    markets.create_market(session, "WOOD", "COIN")

    state = catalog.catalog_state(session)
    by_symbol = {m["symbol"]: m for m in state["markets"]}
    assert by_symbol["FISH"]["place"] == "POST"
    assert by_symbol["WOOD"]["place"] is None
