"""Governance-window tests (docs/game.md §14.4; Phase 2b).

The invariants under test:

  * **The window is derived, never stored** -- ``r % N == 0`` from the
    round counter + deployment config; the calendar endpoint is a pure
    read anyone (script, player) could re-derive.
  * **Cadence bites at enactment, not speech** -- proposals created
    out-of-window are legal but dormant; nothing happens until a window
    round resolves.
  * **Enactment is the clerk's job** -- a POLICY script on a server-owned
    polity, using the ordinary ``enact`` intent; the admin convenience is
    the same path, and a target without the capability is simply rejected
    (a hand, not a second law-making surface).
  * **The sweep fires once per window close** (ctx.state dedup), even
    though round ``r`` stays in round.state for all K ticks of round
    ``r+1``.
"""

from decimal import Decimal

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.governance import (
    is_window_round,
    next_window_round,
    rounds_per_window,
)
from econ.api.main import app
from econengine import services
from econengine.models import (
    Base,
    Entity,
    EntityType,
    Proposal,
    ProposalStatus,
    ProposalType,
    User,
)
from experiments.world.scenario import make_clerk


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "1")
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
        session.add_all([
            User(id="u-admin", email="admin@x", name="Admin",
                 provider="test", provider_id="1", is_admin=True),
            User(id="u-alice", email="alice@x", name="Alice",
                 provider="test", provider_id="2"),
            User(id="u-bob", email="bob@x", name="Bob",
                 provider="test", provider_id="3"),
        ])
        session.commit()

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _session() -> Session:
    return Session(app.state._test_engine)


def _seed_entity(owner_id: str | None, name: str = "E") -> str:
    """An ACTIVE INDIVIDUAL (electorate member when owner is a citizen).
    Returns the entity id -- ORM handles must not outlive their session."""
    with _session() as session:
        entity = services.create_entity(session, name, EntityType.INDIVIDUAL)
        entity.owner_id = owner_id
        services.create_account(session, entity, "USD", Decimal("100"))
        session.commit()
        return entity.id


def _advance(client) -> dict:
    r = client.post("/admin/rounds/advance", headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    return r.json()


def _make_proposal(target_id: str, *, title="A law",
                   weight_model="citizen",
                   mutations=None, proposer=None) -> str:
    """A proposal targeting the given government (default: a plain
    set_fiscal_policy ordinary law that needs no money to apply)."""
    with _session() as session:
        proposer = proposer or _seed_entity(None, "Proposer")
        proposal = services.create_proposal(
            session,
            proposer_id=proposer,
            target_id=target_id,
            title=title,
            weight_model=weight_model,
            threshold=Decimal("0.5"),
            quorum=Decimal("0"),
            mutations=mutations or [{
                "type": "set_fiscal_policy",
                "params": {"setting": {"levy_rate": "0.1"}},
            }],
        )
        session.commit()
        return proposal.id


def _vote(proposal_id: str, voter_id: str, choice: str = "for") -> None:
    """Cast a vote directly (the engine's tally is the authority here;
    vote mechanics are tested elsewhere)."""
    with _session() as session:
        from econengine import weights
        from econengine.models import Vote, VoteChoice
        w = weights.electorate(session, "citizen")[voter_id]
        session.add(Vote(
            proposal_id=proposal_id,
            voter_id=voter_id,
            choice=VoteChoice(choice),
            weight=str(w),
        ))
        session.commit()


# ---------------------------------------------------------------------------
# The derived calendar
# ---------------------------------------------------------------------------

def test_window_derivation_from_env(monkeypatch):
    monkeypatch.delenv("ECON_ROUNDS_PER_WINDOW", raising=False)
    assert rounds_per_window() == 5          # default, like K
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "3")
    assert rounds_per_window() == 3
    assert is_window_round(3) and not is_window_round(4)
    assert is_window_round(6) and is_window_round(0)  # genesis multiple
    assert next_window_round(4) == 6
    assert next_window_round(6) == 6
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "0")
    assert rounds_per_window() == 5          # non-positive falls back


def test_governance_current_is_a_derived_fact(client):
    r = client.get("/governance/current", headers=_auth("u-alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["rounds_per_window"] == 5    # default N
    assert body["round_number"] == 0 and body["current_round"] == 1
    assert body["is_window_round"] is False  # 1 % 5 != 0
    assert body["next_window_round"] == 5
    assert body["rounds_until_window"] == 4
    assert body["open_proposals"] == []

    # ...and it is a pure read: advancing does not change the derivation.
    _advance(client)                          # round 1 resolved
    r = client.get("/governance/current", headers=_auth("u-alice"))
    body = r.json()
    assert body["current_round"] == 2 and body["is_window_round"] is False

    for _ in range(3):
        _advance(client)                      # rounds 2..4
    r = client.get("/governance/current", headers=_auth("u-alice"))
    body = r.json()
    assert body["current_round"] == 5         # resolving it closes a window
    assert body["is_window_round"] is True
    assert body["next_window_round"] == 5 and body["rounds_until_window"] == 0


def test_governance_current_requires_auth(client):
    assert client.get("/governance/current").status_code == 401


def test_docket_lists_open_proposals_with_live_tallies(client):
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    voter1 = _seed_entity(None, "V1")
    voter2 = _seed_entity(None, "V2")
    p = _make_proposal(clerk_id)
    _vote(p, voter1, "for")
    _vote(p, voter2, "against")

    r = client.get("/governance/current", headers=_auth("u-alice"))
    body = r.json()
    assert len(body["open_proposals"]) == 1
    row = body["open_proposals"][0]
    assert row["id"] == p and row["title"] == "A law"
    assert row["proposal_type"] == "ordinary"
    assert Decimal(row["tally"]["yes"]) == Decimal("1")
    assert Decimal(row["tally"]["no"]) == Decimal("1")
    assert Decimal(row["tally"]["electorate"]) > 0


# ---------------------------------------------------------------------------
# Cadence: dormant until the window closes
# ---------------------------------------------------------------------------

def test_out_of_window_proposal_is_legal_but_dormant(client, monkeypatch):
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "2")
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    p = _make_proposal(clerk_id)
    _vote(p, _seed_entity(None, "V1"), "for")   # a majority: would pass

    # Round 1 resolves -- not a window round (1 % 2 != 0). The docket
    # stays dormant despite a passing tally: cadence bites at enactment.
    _advance(client)
    with _session() as session:
        assert session.get(Proposal, p).status == ProposalStatus.OPEN

    # Round 2 resolves -- the window closes. round.state now says 2, and
    # the clerk first sees it on round 3's ticks (advance writes the
    # counter after its batch), so the sweep lands here: enacted.
    _advance(client)   # round 2: window close (counter flips)
    _advance(client)   # round 3: clerk reads round_number=2 -> sweep
    with _session() as session:
        assert session.get(Proposal, p).status == ProposalStatus.ENACTED


def test_clerk_sweeps_once_per_window_close(client, monkeypatch):
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "2")
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    p1 = _make_proposal(clerk_id, title="First law")
    _vote(p1, _seed_entity(None, "V1"), "for")

    _advance(client)   # round 1: dormant
    _advance(client)   # round 2: window closes (counter flips)
    _advance(client)   # round 3: clerk reads round_number=2 -> sweep
    with _session() as session:
        assert session.get(Proposal, p1).status == ProposalStatus.ENACTED

    # A NEW proposal after the sweep rides to the next window: round 2
    # remains visible in round.state for round 3's ticks (the K-tick
    # repetition), but the clerk's ctx.state dedup means no mid-cycle
    # decision; round 3 (odd) is no window either.
    p2 = _make_proposal(clerk_id, title="Second law")
    _vote(p2, _seed_entity(None, "V2"), "for")
    # Round 4's ticks all see round_number=3 (no window), and round 3's
    # later ticks would re-see 2 -- the ctx.state dedup holds: p2 rides.
    _advance(client)   # round 4 (odd): dormant
    with _session() as session:
        assert session.get(Proposal, p2).status == ProposalStatus.OPEN

    _advance(client)   # round 5: clerk reads round_number=4 -> sweep
    with _session() as session:
        assert session.get(Proposal, p2).status == ProposalStatus.ENACTED


def test_failed_tally_at_window_close_decides_the_proposal(client, monkeypatch):
    """Election-day semantics: the sweep decides the whole docket. A
    proposal that fails its tally at the window closes FAILED (with the
    snapshotted reason), not left open forever."""
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "1")
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    p = _make_proposal(clerk_id)
    _vote(p, _seed_entity(None, "V1"), "against")   # a defeat

    _advance(client)   # round 1 (N=1: window) resolves
    _advance(client)   # round 2's ticks read round_number=1 -> sweep
    with _session() as session:
        row = session.get(Proposal, p)
        assert row.status == ProposalStatus.FAILED
        assert row.failure_reason


def test_clerk_without_round_state_is_inert(client):
    """Raw-tick worlds (no scheduler): round.state is absent, the clerk
    returns immediately, the docket never moves. Cadence belongs to the
    round scheduler, not to ticks."""
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    with _session() as session:
        from sqlalchemy import select
        from econengine.tick import run_tick
        proposer = services.create_entity(session, "P", EntityType.INDIVIDUAL)
        services.create_proposal(
            session, proposer_id=proposer.id, target_id=clerk_id,
            title="Never decided", weight_model="citizen",
            threshold=Decimal("0.5"), quorum=Decimal("0"),
            mutations=[{"type": "set_fiscal_policy",
                        "params": {"setting": {"levy_rate": "0.1"}}}],
        )
        for _ in range(3):
            run_tick(session)
        session.commit()
        statuses = session.execute(select(Proposal.status)).scalars().all()
        assert statuses == [ProposalStatus.OPEN]


# ---------------------------------------------------------------------------
# The admin by-election button: same intent path, no second surface
# ---------------------------------------------------------------------------

def test_admin_enact_specific_proposal(client):
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    p = _make_proposal(clerk_id)
    _vote(p, _seed_entity(None, "V1"), "for")

    r = client.post("/admin/governance/enact", headers=_auth("u-admin"),
                    json={"proposal_id": p})
    assert r.status_code == 200
    [outcome] = r.json()
    assert outcome["status"] == "applied"
    assert outcome["proposal_status"] == "enacted"
    with _session() as session:
        assert session.get(Proposal, p).status == ProposalStatus.ENACTED


def test_admin_enact_requires_admin(client):
    r = client.post("/admin/governance/enact", headers=_auth("u-alice"), json={})
    assert r.status_code == 403


def test_admin_enact_404_unknown_and_409_not_open(client):
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    r = client.post("/admin/governance/enact", headers=_auth("u-admin"),
                    json={"proposal_id": "missing"})
    assert r.status_code == 404

    p = _make_proposal(clerk_id)
    _vote(p, _seed_entity(None, "V1"), "for")
    client.post("/admin/governance/enact", headers=_auth("u-admin"),
                json={"proposal_id": p})
    r = client.post("/admin/governance/enact", headers=_auth("u-admin"),
                    json={"proposal_id": p})   # already enacted
    assert r.status_code == 409


def test_admin_enact_sweeps_the_docket_like_the_clerk(client):
    with _session() as session:
        clerk_id = make_clerk(session).id
        session.commit()
    passed = _make_proposal(clerk_id, title="Passes")
    _vote(passed, _seed_entity(None, "V1"), "for")
    failed = _make_proposal(clerk_id, title="Fails")
    _vote(failed, _seed_entity(None, "V2"), "against")

    r = client.post("/admin/governance/enact", headers=_auth("u-admin"), json={})
    assert r.status_code == 200
    outcomes = {(o["params"]["proposal_id"], o["status"]) for o in r.json()}
    assert (passed, "applied") in outcomes
    assert (failed, "applied") in outcomes   # the intent applied; the
    # tally inside decided it:
    with _session() as session:
        assert session.get(Proposal, passed).status == ProposalStatus.ENACTED
        assert session.get(Proposal, failed).status == ProposalStatus.FAILED


def test_admin_enact_cannot_bypass_capability(client):
    """The button is a hand, not a surface: enacting as a target that does
    NOT hold LEGISLATE is rejected by the ordinary capability gate."""
    with _session() as session:
        weak_id = services.create_entity(session, "Weak Gov", EntityType.GOVERNMENT).id
        session.commit()
    p = _make_proposal(weak_id)
    _vote(p, _seed_entity(None, "V1"), "for")

    r = client.post("/admin/governance/enact", headers=_auth("u-admin"),
                    json={"proposal_id": p})
    assert r.status_code == 200
    [outcome] = r.json()
    assert outcome["status"] == "rejected"
    assert "legislate" in outcome["reason"]
    with _session() as session:
        assert session.get(Proposal, p).status == ProposalStatus.OPEN


# ---------------------------------------------------------------------------
# round.state carries N (the script-visible channel)
# ---------------------------------------------------------------------------

def test_round_state_carries_rounds_per_window(client, monkeypatch):
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "3")
    _advance(client)
    from econ.api.rounds import ROUND_STATE_KEY
    from econengine.models import WorldSetting
    with _session() as session:
        value = dict(session.get(WorldSetting, ROUND_STATE_KEY).value)
    assert value["round_number"] == 1
    assert value["rounds_per_window"] == 3


def test_clerk_enacts_via_round_state_channel(client, monkeypatch):
    """End to end: the clerk derives the calendar ONLY from round.state
    (the N it carries + the counter), no env in Lua -- so an env change
    takes effect at the next advance's projection."""
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "2")
    with _session() as session:
        make_clerk(session)
        session.commit()
    p = _make_proposal(_last_government())
    _vote(p, _seed_entity(None, "V1"), "for")

    _advance(client)   # round 1: 1 % 2 != 0 -> dormant
    _advance(client)   # round 2's ticks read round_number=1 -> dormant
    with _session() as session:
        assert session.get(Proposal, p).status == ProposalStatus.OPEN
    _advance(client)   # round 2 resolves (window)
    _advance(client)   # round 3's ticks read round_number=2 -> sweep
    with _session() as session:
        assert session.get(Proposal, p).status == ProposalStatus.ENACTED


def _last_government() -> str:
    with _session() as session:
        from sqlalchemy import select
        gov = session.execute(
            select(Entity).where(Entity.entity_type == EntityType.GOVERNMENT)
            .order_by(Entity.name, Entity.id)
        ).scalars().first()
        return gov.id


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

def test_mcp_governance_current_tool(client, monkeypatch):
    monkeypatch.setenv("ECON_ROUNDS_PER_WINDOW", "2")
    with _session() as session:
        make_clerk(session)
        session.commit()
    p = _make_proposal(_last_government())
    _vote(p, _seed_entity(None, "V1"), "for")

    import json
    r = client.post("/mcp", headers=_auth("u-alice"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "governance_current", "arguments": {}},
    })
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["rounds_per_window"] == 2
    assert payload["is_window_round"] is False       # round 1 open, 1%2!=0
    assert payload["next_window_round"] == 2
    assert payload["open_proposals"][0]["id"] == p

    r = client.post("/mcp", headers=_auth("u-alice"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "governance_current" in names
