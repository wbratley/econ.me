"""The catalog render (Phase 3a, game.md §15.1).

The doctrine under test: **derived where derivable, authored where
meaningful.** A condition's effect line is generated from its row --
"granted 1 per fully-unmet FOOD tick; decays 5%/tick (equilibrium ≈ 20
held); incapacitates at 15" -- and prose cannot drift from physics
because the prose is a function of the physics. The same render backs
``GET /catalog`` and MCP ``world_catalog`` (parity: the prompt and the
script read the same catalog).
"""
from decimal import Decimal

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econengine import goods, markets, needs, production, tech
from econengine.catalog import catalog_state, catalog_text
from econengine.models import Base, EntityType
from econengine.tech import TechScope


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session):
    """A pocket world exercising every derivable line."""
    goods.create_good(
        session, "LABOR", name="Labor", decay_per_tick=Decimal("0.5"),
        auto_issue_quantity=Decimal("1"),
        auto_issue_entity_type=EntityType.INDIVIDUAL,
    )
    goods.create_good(session, "BERRIES", name="Berries",
                      decay_per_tick=Decimal("0.25"))
    goods.create_good(session, "WOOD", name="Wood")
    goods.create_good(
        session, "HUNGER", decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("15"),
    )
    goods.create_good(session, "COLD_HANDS", modifies_pattern="LABOR-*",
                      modifies_factor=Decimal("0.5"))
    needs.create_need(
        session, "FOOD", Decimal("1"), ["BERRIES", "JERKY"],
        condition_symbol="HUNGER", condition_quantity=Decimal("1"),
    )
    tech.create_technology(session, "KNAPPING", scope=TechScope.ENTITY)
    production.create_recipe(
        session, "GATHER", inputs={"LABOR": Decimal("1")}, outputs={},
        duration_ticks=1,
        branches=[
            {"weight": Decimal("45"), "outputs": {"BERRIES": Decimal("3")},
             "label": "berries"},
            {"weight": Decimal("55"), "outputs": {}, "label": "nothing"},
        ],
    )
    production.create_recipe(
        session, "MAKE_SPEAR", inputs={"LABOR": Decimal("1"), "FLINT": Decimal("1")},
        outputs={"SPEAR": Decimal("1")}, duration_ticks=2,
        requires_facility="CAMP", requires=["KNAPPING"],
        good_requirements={"BAG": Decimal("1")},
    )
    markets.create_market(session, "BERRIES", "COIN", name="Berries")


def test_condition_effect_line_is_derived_from_the_row(session):
    _seed(session)
    state = catalog_state(session)
    hunger = next(g for g in state["goods"] if g["symbol"] == "HUNGER")
    # §15.1's own example: grant, decay, equilibrium, incapacitation --
    # every number read off the rows, nothing hand-written.
    assert hunger["effect"] == (
        "granted 1 per fully-unmet FOOD tick (scaled by shortfall); "
        "decays 5%/tick; equilibrium ≈ 20 held; incapacitates at 15"
    )


def test_goods_rows_flag_conditions_machine_readably(session):
    """The catalog's condition flag: consumers (snapshots, dashboards)
    split condition goods from commodity inventory without parsing the
    prose effect line."""
    _seed(session)
    state = catalog_state(session)
    by_symbol = {g["symbol"]: g["condition"] for g in state["goods"]}
    assert by_symbol["HUNGER"] is True          # incapacitates_at
    assert by_symbol["COLD_HANDS"] is True       # modifies_pattern
    assert by_symbol["BERRIES"] is False
    assert by_symbol["WOOD"] is False


def test_plain_goods_render_their_physics_only(session):
    _seed(session)
    state = catalog_state(session)
    by_symbol = {g["symbol"]: g for g in state["goods"]}
    assert by_symbol["BERRIES"]["effect"] == "decays 25%/tick"
    assert by_symbol["WOOD"]["effect"] is None
    assert by_symbol["LABOR"]["effect"] == (
        "decays 50%/tick; auto-issued up to 1/tick to every individual"
    )
    assert by_symbol["COLD_HANDS"]["effect"] == (
        "while held, effective LABOR-* × 0.5"
    )


def test_need_renders_draw_order_and_condition_link(session):
    _seed(session)
    state = catalog_state(session)
    food = next(n for n in state["needs"] if n["code"] == "FOOD")
    assert food["draws"] == (
        "draws 1/tick from holdings, eating BERRIES, then JERKY; "
        "tried in that order, each unit covers one tick"
    )
    assert food["condition"] == {"symbol": "HUNGER", "quantity": "1"}


def test_recipe_renders_branch_odds_gates_and_costs(session):
    _seed(session)
    state = catalog_state(session)
    by_code = {r["code"]: r for r in state["recipes"]}

    gather = by_code["GATHER"]
    assert gather["line"] == (
        "1 LABOR → 45%: 3 BERRIES (berries); 55%: nothing (nothing)"
    )
    assert gather["effects"] == ["takes 1 tick"]

    spear = by_code["MAKE_SPEAR"]
    assert spear["line"] == "1 FLINT + 1 LABOR → 1 SPEAR"
    assert spear["effects"] == [
        "takes 2 ticks",
        "must run at a CAMP facility",
        "requires the KNAPPING technology (entity-scoped)",
        "holds ≥ 1 BAG to run (reserved, not consumed)",
    ]


def test_market_renders_name_and_currency(session):
    _seed(session)
    state = catalog_state(session)
    assert state["markets"] == [
        {"symbol": "BERRIES", "name": "Berries", "description": "",
         "pack": None, "currency": "COIN"}
    ]


def test_tech_renders_scope_and_prerequisites(session):
    _seed(session)
    state = catalog_state(session)
    knapping = next(t for t in state["technologies"] if t["code"] == "KNAPPING")
    assert knapping["scope"] == "entity"
    assert knapping["requires"] == []


# --- The shipped surfaces: GET /catalog and MCP world_catalog (parity) ----


@pytest.fixture
def api_client(session):
    from fastapi import Depends, HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from fastapi.testclient import TestClient

    from econ.api.deps import bearer_scheme, get_current_user, get_session
    from econ.api.main import app
    from econengine.models import User

    session.add(User(id="u-alice", email="alice@x", name="Alice",
                     provider="test", provider_id="1"))
    session.commit()
    app.state._test_engine = session.get_bind()

    def override_get_session():
        yield session

    def override_get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
        s: Session = Depends(get_session),
    ) -> User:
        user = s.get(User, credentials.credentials)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_catalog_endpoint_serves_the_read(api_client, session):
    _seed(session)
    r = api_client.get("/catalog", headers={"Authorization": "Bearer u-alice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {g["symbol"] for g in body["goods"]} >= {"HUNGER", "BERRIES", "WOOD"}
    assert r.json()["goods"] == catalog_state(session)["goods"]


def test_mcp_world_catalog_is_the_same_read(api_client, session):
    _seed(session)
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "world_catalog", "arguments": {}},
    }
    r = api_client.post("/mcp", headers={"Authorization": "Bearer u-alice"},
                        json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    # The tool surfaces the same render as the endpoint (§13 parity).
    tool_data = json.loads(body["result"]["content"][0]["text"])
    assert tool_data == catalog_state(session)


def test_catalog_requires_auth(api_client):
    r = api_client.get("/catalog")
    assert r.status_code in (401, 403)


def test_catalog_text_is_the_prompt_fold(session):
    """The compact prose render: every section present, derived numbers
    carried through (costs, odds, gates, death thresholds) — the same
    shared read the REST catalog serves, as plain text for prompts."""
    _seed(session)
    state = catalog_state(session)
    text = catalog_text(state)
    assert "== GOODS (what exists; what holding or issuing it does) ==" in text
    assert "- BERRIES (Berries)" in text          # name rides
    assert "decays 25%/tick" in text              # derived effect
    assert "== NEEDS (drawn every tick; shortfalls bite) ==" in text
    assert "draws 1/tick from holdings, eating" in text  # the draw-order line renders
    assert "== THE ACTION SPACE (recipes: inputs -> outputs) ==" in text
    assert "GATHER" in text
    assert "== MARKETS (order books; quote currencies) ==" in text
    assert "BERRIES/COIN" in text
