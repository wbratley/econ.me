"""Access-token TTL is deployment config, not a law of nature.

The default (60 minutes) is a human-session default for the public site.
A harness that boots its own world for a multi-hour dynasty run needs
tokens that outlive the run — so the TTL rides the environment, same
doctrine as SECRET_KEY. (A 20-round NIM dynasty run crossed 60 minutes
and every set_ready bounced 401; this test pins the knob that fixed it.)
"""

import importlib
from datetime import datetime, timezone

import pytest


def _auth_module(monkeypatch, env_value=None):
    if env_value is None:
        monkeypatch.delenv("ACCESS_TOKEN_EXPIRE_MINUTES", raising=False)
    else:
        monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", env_value)
    import econ.api.auth as auth
    return importlib.reload(auth)


@pytest.fixture(autouse=True)
def _restore_auth_module():
    """reload() swaps module constants process-wide; put them back so
    later tests (and the app under test elsewhere) see the defaults."""
    import econ.api.auth as auth
    yield
    importlib.reload(auth)


def test_default_ttl_is_an_hour(monkeypatch):
    auth = _auth_module(monkeypatch)                 # env unset
    assert auth.ACCESS_TOKEN_EXPIRE_MINUTES == 60


def test_ttl_rides_the_environment(monkeypatch):
    auth = _auth_module(monkeypatch, "100000")
    assert auth.ACCESS_TOKEN_EXPIRE_MINUTES == 100000
    before = datetime.now(timezone.utc)
    token = auth.create_token("u-admin", "admin@run", True)
    claims = auth.decode_token(token)
    minutes = (datetime.fromtimestamp(claims["exp"], timezone.utc) - before
               ).total_seconds() / 60
    assert minutes > 99990                            # minted with the override


def test_bad_ttl_falls_over_loudly(monkeypatch):
    with pytest.raises(ValueError):
        _auth_module(monkeypatch, "not-a-number")
