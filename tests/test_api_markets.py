"""
End-to-end market flow through the HTTP API: admin creates a market and
grants holdings, users place orders from their own accounts, an admin tick
clears the auction, and trades/balances/holdings are visible afterwards.

Auth is stubbed at the dependency level: the bearer token IS the user id.
"""

import pytest
from decimal import Decimal
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.models import Base, User


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    def override_get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
        session: Session = Depends(get_session),
    ) -> User:
        user = session.get(User, credentials.credentials)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    with Session(engine) as session:
        users = {
            "admin": User(id="u-admin", email="admin@x", name="Admin",
                          provider="test", provider_id="1", is_admin=True),
            "buyer": User(id="u-buyer", email="buyer@x", name="Buyer",
                          provider="test", provider_id="2"),
            "seller": User(id="u-seller", email="seller@x", name="Seller",
                           provider="test", provider_id="3"),
        }
        session.add_all(users.values())
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _make_trader(client, user_id: str, name: str, balance: str) -> tuple[str, str]:
    """Create an entity + USD account for a user; returns (entity_id, account_id)."""
    r = client.post("/entities", json={"name": name, "entity_type": "individual"},
                    headers=_auth(user_id))
    assert r.status_code == 201, r.text
    entity_id = r.json()["id"]
    r = client.post(f"/entities/{entity_id}/accounts",
                    json={"currency": "USD", "initial_balance": balance},
                    headers=_auth(user_id))
    assert r.status_code == 201, r.text
    return entity_id, r.json()["id"]


def test_full_market_flow(client):
    # Admin creates the market and grants the seller some wheat.
    r = client.post("/admin/markets",
                    json={"symbol": "wheat", "currency": "usd", "name": "Wheat"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    assert r.json()["symbol"] == "WHEAT"
    assert r.json()["last_price"] is None

    buyer_entity, buyer_account = _make_trader(client, "u-buyer", "Buyer Co", "1000")
    seller_entity, seller_account = _make_trader(client, "u-seller", "Seller Co", "0")

    r = client.post("/admin/holdings",
                    json={"entity_id": seller_entity, "symbol": "WHEAT", "delta": "100"},
                    headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["quantity"]) == Decimal("100")

    # Both sides place crossing orders from their own accounts.
    r = client.post("/orders",
                    json={"symbol": "WHEAT", "side": "sell", "quantity": "50",
                          "limit_price": "10", "account_id": seller_account},
                    headers=_auth("u-seller"))
    assert r.status_code == 201, r.text
    sell_order = r.json()
    assert sell_order["status"] == "open"

    r = client.post("/orders",
                    json={"symbol": "WHEAT", "side": "buy", "quantity": "50",
                          "limit_price": "10", "account_id": buyer_account},
                    headers=_auth("u-buyer"))
    assert r.status_code == 201, r.text
    buy_order = r.json()

    # Admin advances the tick — the call auction clears.
    r = client.post("/admin/ticks", headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    events = r.json()["events"]
    assert any(e["type"] == "auction" and e["market"] == "WHEAT" for e in events)
    assert sum(1 for e in events if e["type"] == "trade") == 2  # one per side

    # Trade is visible, market has a last price.
    r = client.get("/markets/WHEAT/trades", headers=_auth("u-buyer"))
    assert r.status_code == 200
    trades = r.json()
    assert len(trades) == 1
    assert trades[0]["price"] == "10.0000"
    assert trades[0]["quantity"] == "50.0000"
    assert trades[0]["buy_order_id"] == buy_order["id"]
    assert trades[0]["sell_order_id"] == sell_order["id"]

    r = client.get("/markets/WHEAT", headers=_auth("u-seller"))
    assert r.json()["last_price"] == "10.0000"

    # Goods and money actually moved.
    r = client.get(f"/entities/{buyer_entity}/holdings", headers=_auth("u-buyer"))
    assert [(h["symbol"], h["quantity"]) for h in r.json()] == [("WHEAT", "50.0000")]

    r = client.get(f"/entities/{buyer_entity}", headers=_auth("u-buyer"))
    assert r.json()["accounts"][0]["balance"] == "500.0000"

    r = client.get(f"/entities/{seller_entity}", headers=_auth("u-seller"))
    assert r.json()["accounts"][0]["balance"] == "500.0000"

    r = client.get("/admin/holdings", params={"entity_id": seller_entity},
                   headers=_auth("u-admin"))
    assert [(h["symbol"], h["quantity"]) for h in r.json()] == [("WHEAT", "50.0000")]

    # Both orders show as filled to their owners.
    r = client.get("/orders", headers=_auth("u-buyer"))
    assert [o["status"] for o in r.json()] == ["filled"]


def test_order_auth_boundaries(client):
    client.post("/admin/markets", json={"symbol": "WHEAT", "currency": "USD"},
                headers=_auth("u-admin"))
    _, buyer_account = _make_trader(client, "u-buyer", "Buyer Co", "100")

    # Can't order out of someone else's account.
    r = client.post("/orders",
                    json={"symbol": "WHEAT", "side": "buy", "quantity": "1",
                          "limit_price": "1", "account_id": buyer_account},
                    headers=_auth("u-seller"))
    assert r.status_code == 403

    # Can't cancel someone else's order.
    r = client.post("/orders",
                    json={"symbol": "WHEAT", "side": "buy", "quantity": "1",
                          "limit_price": "1", "account_id": buyer_account},
                    headers=_auth("u-buyer"))
    order_id = r.json()["id"]
    r = client.post(f"/orders/{order_id}/cancel", headers=_auth("u-seller"))
    assert r.status_code == 404

    # Owner can cancel; a second cancel is idempotent (the caller's
    # success state is "nothing rests", whether or not it already did).
    r = client.post(f"/orders/{order_id}/cancel", headers=_auth("u-buyer"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    r = client.post(f"/orders/{order_id}/cancel", headers=_auth("u-buyer"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Admin endpoints are admin-only.
    r = client.post("/admin/markets", json={"symbol": "OIL", "currency": "USD"},
                    headers=_auth("u-buyer"))
    assert r.status_code == 403


def test_order_validation_errors(client):
    client.post("/admin/markets", json={"symbol": "WHEAT", "currency": "USD"},
                headers=_auth("u-admin"))
    _, account = _make_trader(client, "u-buyer", "Buyer Co", "100")

    for bad in (
        {"symbol": "NOPE", "side": "buy", "quantity": "1", "limit_price": "1"},
        {"symbol": "WHEAT", "side": "buy", "quantity": "0", "limit_price": "1"},
        {"symbol": "WHEAT", "side": "buy", "quantity": "1", "limit_price": "-2"},
        {"symbol": "WHEAT", "side": "buy", "quantity": "abc", "limit_price": "1"},
    ):
        r = client.post("/orders", json={**bad, "account_id": account},
                        headers=_auth("u-buyer"))
        assert r.status_code == 422, bad

    # Duplicate market symbol → 409; deactivated market rejects orders.
    r = client.post("/admin/markets", json={"symbol": "WHEAT", "currency": "USD"},
                    headers=_auth("u-admin"))
    assert r.status_code == 409

    r = client.patch("/admin/markets/WHEAT", json={"is_active": False},
                     headers=_auth("u-admin"))
    assert r.status_code == 200 and r.json()["is_active"] is False

    r = client.post("/orders",
                    json={"symbol": "WHEAT", "side": "buy", "quantity": "1",
                          "limit_price": "1", "account_id": account},
                    headers=_auth("u-buyer"))
    assert r.status_code == 422
