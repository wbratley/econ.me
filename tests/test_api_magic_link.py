"""API tests for email magic-link login (POST /auth/magic/request →
GET /auth/magic/verify → POST /auth/magic/exchange).

Identity is the email address: a magic sign-in lands on the same account
an OAuth login with that address created. With no SMTP configured the
link is logged to the server console (the dev loop); the exchange-code
detour keeps the access token itself out of URLs. Purpose-scoped tokens
pin the safety property: a magic link or exchange code must never work
as a bearer token.
"""

import logging
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.auth import create_magic_token, create_token, decode_token
from econ.api.deps import get_session
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
    app.state._test_engine = engine

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    with Session(engine) as session:
        session.add(User(id="u-oauth", email="alice@x", name="Alice",
                         provider="google", provider_id="g-1"))
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _logged_link(caplog) -> str:
    record = next(r for r in caplog.records if "magic sign-in link" in r.getMessage())
    return record.getMessage().split(": ", 1)[1]


def _verify(client: TestClient, url: str) -> str:
    """Redeem a magic link; return the exchange code from the redirect."""
    token = re.search(r"token=(\S+)$", url).group(1)
    response = client.get(f"/auth/magic/verify?token={token}", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("http://localhost:5173/")
    return re.search(r"code=(\S+)$", location).group(1)


def test_request_creates_user_and_logs_link(client, caplog):
    caplog.set_level(logging.WARNING, logger="econ.api.routers.auth")
    response = client.post("/auth/magic/request", json={"email": "New@Example.com"})
    assert response.status_code == 200

    with Session(app.state._test_engine) as session:
        user = session.query(User).filter_by(email="new@example.com").one()
        assert user.provider == "magic"           # address normalized

    code = _verify(client, _logged_link(caplog))
    exchanged = client.post("/auth/magic/exchange", json={"code": code})
    assert exchanged.status_code == 200
    claims = decode_token(exchanged.json()["access_token"])
    assert claims["sub"] == user.id
    assert claims["email"] == "new@example.com"
    assert claims["is_admin"] is False


def test_request_lands_on_the_oauth_account_with_that_email(client, caplog):
    """Magic sign-in is account-by-email, not a parallel account."""
    caplog.set_level(logging.WARNING, logger="econ.api.routers.auth")
    assert client.post("/auth/magic/request", json={"email": "alice@x"}).status_code == 200

    code = _verify(client, _logged_link(caplog))
    exchanged = client.post("/auth/magic/exchange", json={"code": code})
    claims = decode_token(exchanged.json()["access_token"])
    assert claims["sub"] == "u-oauth"

    with Session(app.state._test_engine) as session:
        assert session.query(User).filter_by(email="alice@x").count() == 1


def test_request_rejects_malformed_email(client):
    assert client.post("/auth/magic/request", json={"email": "nope"}).status_code == 400


def test_verify_rejects_garbage_and_access_tokens(client):
    assert client.get("/auth/magic/verify?token=garbage",
                      follow_redirects=False).status_code == 400
    access = create_token("u-oauth", "alice@x", False)
    assert client.get("/auth/magic/verify", params={"token": access},
                      follow_redirects=False).status_code == 400


def test_exchange_rejects_garbage_and_magic_tokens(client):
    assert client.post("/auth/magic/exchange", json={"code": "garbage"}).status_code == 400
    magic = create_magic_token("u-oauth")
    assert client.post("/auth/magic/exchange", json={"code": magic}).status_code == 400


def test_magic_token_is_not_a_bearer_token(client):
    """The purpose claim walls sign-in tokens off from the auth surface."""
    magic = create_magic_token("u-oauth")
    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {magic}"}).status_code == 401
