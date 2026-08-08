"""Machine-client intent API (design.md §4.5): POST /intents resolves a
batch of intents through the same scripting.resolve_intent dispatcher
Lua scripts and the tick engine already share, instead of the human
per-action REST endpoints."""

import json

import pytest
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
        session.add_all([
            User(id="u-admin", email="admin@x", name="Admin",
                 provider="test", provider_id="1", is_admin=True),
            User(id="u-alice", email="alice@x", name="Alice",
                 provider="test", provider_id="2"),
            User(id="u-mallory", email="mallory@x", name="Mallory",
                 provider="test", provider_id="3"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _make_entity(client, owner, name="Alice Co"):
    r = client.post("/entities", json={"name": name, "entity_type": "individual"},
                    headers=_auth(owner))
    assert r.status_code == 201, r.text
    return r.json()


def _make_account(client, owner, entity_id, currency="USD", initial_balance="1000"):
    r = client.post(f"/entities/{entity_id}/accounts",
                    json={"currency": currency, "initial_balance": initial_balance},
                    headers=_auth(owner))
    assert r.status_code == 201, r.text
    return r.json()


def test_batch_applies_owned_entity_intents(client):
    alice = _make_entity(client, "u-alice")
    a = _make_account(client, "u-alice", alice["id"])
    bob = _make_entity(client, "u-alice", "Bob")
    b = _make_account(client, "u-alice", bob["id"], initial_balance="0")

    client.post("/admin/recipes",
                json={"code": "FORAGE", "duration_ticks": 1, "outputs": {"BERRIES": "1"}},
                headers=_auth("u-admin"))

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": b["id"],
                    "amount": "100", "reference": "rent"}},
        {"entity_id": alice["id"], "type": "start_process",
         "params": {"recipe": "FORAGE"}},
    ], headers=_auth("u-alice"))

    assert r.status_code == 200, r.text
    results = r.json()
    assert [item["status"] for item in results] == ["applied", "applied"]
    assert results[0]["type"] == "transfer"
    assert results[1]["process_id"]

    r = client.get(f"/entities/{alice['id']}/accounts/{a['id']}/transactions",
                    headers=_auth("u-alice"))
    assert any(t["amount"] == "100.0000" and t["tx_type"] == "debit" for t in r.json())


def test_batch_rejects_entity_the_caller_does_not_own(client):
    alice = _make_entity(client, "u-alice")
    a = _make_account(client, "u-alice", alice["id"])

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": a["id"],
                    "amount": "1", "reference": "steal"}},
    ], headers=_auth("u-mallory"))

    assert r.status_code == 200
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert result["reason"] == "entity not found"


def test_batch_rejects_unknown_intent_type(client):
    alice = _make_entity(client, "u-alice")

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "teleport", "params": {}},
    ], headers=_auth("u-alice"))

    assert r.status_code == 200
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "teleport" in result["reason"]


def test_batch_isolates_a_failing_intent_from_the_rest(client):
    alice = _make_entity(client, "u-alice")
    a = _make_account(client, "u-alice", alice["id"], initial_balance="50")
    bob = _make_entity(client, "u-alice", "Bob")
    b = _make_account(client, "u-alice", bob["id"], initial_balance="0")

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": b["id"],
                    "amount": "999999", "reference": "too much"}},
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": b["id"],
                    "amount": "10", "reference": "fine"}},
    ], headers=_auth("u-alice"))

    assert r.status_code == 200
    results = r.json()
    assert results[0]["status"] == "rejected"
    assert results[1]["status"] == "applied"

    r = client.get(f"/entities/{bob['id']}/accounts/{b['id']}/transactions",
                    headers=_auth("u-alice"))
    assert [t["amount"] for t in r.json()] == ["10.0000"]


def test_levy_intent_seizes_through_the_api(client):
    """The full HTTP path for enforced collection: a levy-capable
    government (owned by admin) compels money out of a citizen's account
    it does not own, into its treasury. Capability gates at the boundary;
    ownership of the source is bypassed by privilege + rule_ref."""
    # a citizen with a funded account (owned by alice)
    citizen = _make_entity(client, "u-alice", "Citizen")
    ca = _make_account(client, "u-alice", citizen["id"], initial_balance="1000")

    # a government entity owned by admin, granted the levy capability
    r = client.post("/admin/entities",
                    json={"name": "Treasury", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    gov = r.json()
    r = client.patch(f"/admin/entities/{gov['id']}",
                     json={"capabilities": ["levy"]}, headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    ga = _make_account(client, "u-admin", gov["id"], initial_balance="0")

    # admin drives the government to levy the citizen
    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "levy",
         "params": {"from_account_id": ca["id"], "to_account_id": ga["id"],
                    "amount": "300", "rule_ref": "tax:income", "reference": "Q1"}},
    ], headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    result = r.json()[0]
    assert result["status"] == "applied", result

    citizen_bal = client.get(f"/entities/{citizen['id']}",
                             headers=_auth("u-alice")).json()["accounts"][0]["balance"]
    gov_bal = client.get(f"/entities/{gov['id']}",
                         headers=_auth("u-admin")).json()["accounts"][0]["balance"]
    assert citizen_bal == "700.0000"   # seized
    assert gov_bal == "300.0000"        # collected


def test_levy_intent_rejected_without_capability(client):
    """A government with no levy capability cannot seize through the API."""
    citizen = _make_entity(client, "u-alice", "Citizen")
    ca = _make_account(client, "u-alice", citizen["id"], initial_balance="1000")
    r = client.post("/admin/entities",
                    json={"name": "WeakGov", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    gov = r.json()  # no capabilities granted
    ga = _make_account(client, "u-admin", gov["id"], initial_balance="0")

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "levy",
         "params": {"from_account_id": ca["id"], "to_account_id": ga["id"],
                    "amount": "300", "rule_ref": "tax:income"}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "levy" in result["reason"]
    # citizen untouched
    citizen_bal = client.get(f"/entities/{citizen['id']}",
                             headers=_auth("u-alice")).json()["accounts"][0]["balance"]
    assert citizen_bal == "1000.0000"


def test_set_fiscal_policy_intent_sets_votable_rate(client):
    """The HTTP path for enacting fiscal policy: a set_fiscal_policy-
    capable government (owned by admin) replaces the votable policy dict.
    Capability gates at the boundary; this is Fork 4B — the power to set
    policy is held by the role, not by a superuser."""
    r = client.post("/admin/entities",
                    json={"name": "Treasury", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    gov = r.json()
    r = client.patch(f"/admin/entities/{gov['id']}",
                     json={"capabilities": ["set_fiscal_policy"]},
                     headers=_auth("u-admin"))
    assert r.status_code == 200

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "set_fiscal_policy",
         "params": {"policy": '{"rate": "0.10", "rule": "income"}'}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "applied", result


def test_set_fiscal_policy_intent_rejected_without_capability(client):
    """A government with no set_fiscal_policy capability cannot enact
    policy through the API."""
    r = client.post("/admin/entities",
                    json={"name": "WeakGov", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    gov = r.json()  # no capabilities

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "set_fiscal_policy",
         "params": {"policy": '{"rate": "0.10"}'}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "set_fiscal_policy" in result["reason"]


def test_set_script_intent_enacts_a_law_through_the_api(client):
    """The full HTTP path for governed lawmaking (step 4a-1): a
    legislate-capable government (owned by admin) enacts a new POLICY law
    via POST /intents. Capability gates at the boundary; the result carries
    the new script's id and lineage, and the law is live (active) in the
    scripts table."""
    # a government entity owned by admin, granted the legislate capability
    r = client.post("/admin/entities",
                    json={"name": "Legislature", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    gov = r.json()
    r = client.patch(f"/admin/entities/{gov['id']}",
                     json={"capabilities": ["legislate"]}, headers=_auth("u-admin"))
    assert r.status_code == 200, r.text

    # admin drives the government to enact a wealth-tax law
    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "set_script",
         "params": {"script_type": "policy", "lineage_id": "wealth_tax",
                    "source": "-- wealth tax law", "reference": "enactment #1"}},
    ], headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    result = r.json()[0]
    assert result["status"] == "applied", result
    assert result["lineage_id"] == "wealth_tax"
    script_id = result["script_id"]

    # the law is live: the active script of the lineage is in the table
    scripts = client.get("/admin/scripts?is_active=true",
                         headers=_auth("u-admin")).json()
    matching = [s for s in scripts if s["lineage_id"] == "wealth_tax"]
    assert len(matching) == 1
    assert matching[0]["id"] == script_id
    assert matching[0]["name"] == "wealth_tax#1"   # auto-versioned per row


def test_set_script_intent_rejected_without_capability(client):
    """A government with no legislate capability cannot enact law through the
    API — the capability, not the API call, authorises lawmaking."""
    r = client.post("/admin/entities",
                    json={"name": "WeakGov", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    gov = r.json()  # no capabilities granted

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "set_script",
         "params": {"script_type": "policy", "lineage_id": "law",
                    "source": "ctx.state.x = 1"}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "legislate" in result["reason"]
    # no law was created
    scripts = client.get("/admin/scripts", headers=_auth("u-admin")).json()
    assert not any(s["lineage_id"] == "law" for s in scripts)


# ---------------------------------------------------------------------------
# democracy layer (actors step 4a-ii) — the full HTTP path
# ---------------------------------------------------------------------------

def test_democracy_full_cycle_enacts_fiscal_policy_via_api(client):
    """Citizens propose, vote, and the government enacts — all through
    POST /intents. Participation is the electorate (citizenship), not a
    capability; enactment is gated on the government's legislate capability."""
    gov = client.post("/admin/entities",
                      json={"name": "Polity", "entity_type": "government",
                            "owner_id": "u-admin"},
                      headers=_auth("u-admin")).json()
    client.patch(f"/admin/entities/{gov['id']}",
                 json={"capabilities": ["legislate", "set_fiscal_policy"]},
                 headers=_auth("u-admin"))
    citizens = [
        client.post("/admin/entities",
                    json={"name": n, "entity_type": "individual", "owner_id": "u-admin"},
                    headers=_auth("u-admin")).json()
        for n in ("A", "B", "C")
    ]

    mutations = json.dumps([
        {"type": "set_fiscal_policy", "params": {"policy": '{"rate":"0.2"}'}}
    ])
    # citizen A proposes a 20% tax
    r = client.post("/intents", json=[
        {"entity_id": citizens[0]["id"], "type": "create_proposal",
         "params": {"target_id": gov["id"], "mutations": mutations,
                    "weight_model": "citizen", "threshold": "0.5", "quorum": "0",
                    "title": "20% tax"}},
    ], headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()[0]["status"] == "applied"
    pid = r.json()[0]["proposal_id"]

    # A and B for, C against -> simple majority of cast weight
    for idx, choice in ((0, "for"), (1, "for"), (2, "against")):
        r = client.post("/intents", json=[
            {"entity_id": citizens[idx]["id"], "type": "vote",
             "params": {"proposal_id": pid, "choice": choice}},
        ], headers=_auth("u-admin"))
        assert r.json()[0]["status"] == "applied", r.text

    # the government enacts the passed proposal
    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "enact",
         "params": {"proposal_id": pid}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "applied", result
    assert result["proposal_status"] == "enacted"

    # admin read side: the proposal and its votes
    got = client.get(f"/admin/proposals/{pid}", headers=_auth("u-admin")).json()
    assert got["status"] == "enacted" and got["tally_yes"] == "2"
    assert len(client.get(f"/admin/proposals/{pid}/votes",
                          headers=_auth("u-admin")).json()) == 3


def test_democracy_rejects_non_citizen_proposer(client):
    """A business is not in the citizen electorate, so it cannot propose —
    participation is membership, defined by the weight model."""
    gov = client.post("/admin/entities",
                      json={"name": "Polity", "entity_type": "government",
                            "owner_id": "u-admin"},
                      headers=_auth("u-admin")).json()
    biz = client.post("/admin/entities",
                      json={"name": "Acme", "entity_type": "business",
                            "owner_id": "u-admin"},
                      headers=_auth("u-admin")).json()
    mutations = json.dumps([
        {"type": "set_fiscal_policy", "params": {"policy": '{"rate":"0.1"}'}}
    ])
    r = client.post("/intents", json=[
        {"entity_id": biz["id"], "type": "create_proposal",
         "params": {"target_id": gov["id"], "mutations": mutations,
                    "weight_model": "citizen"}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "electorate" in result["reason"]
    # nothing was recorded
    assert client.get("/admin/proposals", headers=_auth("u-admin")).json() == []


# ---------------------------------------------------------------------------
# constitutional tier (actors step 4b) — the HTTP path
# ---------------------------------------------------------------------------

def test_constitutional_amendment_installs_a_validator_via_api(client):
    """Citizens amend the constitution over POST /intents: a constitutional
    proposal carries a set_validator mutation, clears the supermajority, and
    the installed validator then binds a direct over-cap op — the same
    safety the engine layer proves, reachable from the machine client."""
    gov = client.post("/admin/entities",
                      json={"name": "Polity", "entity_type": "government",
                            "owner_id": "u-admin"},
                      headers=_auth("u-admin")).json()
    # the government may legislate, set fiscal policy, AND amend the charter
    client.patch(f"/admin/entities/{gov['id']}",
                 json={"capabilities": ["legislate", "set_fiscal_policy",
                                        "amend_constitution"]},
                 headers=_auth("u-admin"))
    citizens = [
        client.post("/admin/entities",
                    json={"name": n, "entity_type": "individual", "owner_id": "u-admin"},
                    headers=_auth("u-admin")).json()
        for n in ("A", "B", "C")
    ]

    cap = (
        "if ctx.op.type == 'set_fiscal_policy' then "
        "local r = tonumber(ctx.op.policy.rate) "
        "if r and r > 0.5 then "
        "return {allow=false, reason='over the cap'} end end"
    )
    mutations = json.dumps([
        {"type": "set_validator", "params": {"lineage_id": "cap", "source": cap}}
    ])
    # citizen A proposes a constitutional amendment (default floor is 2/3)
    r = client.post("/intents", json=[
        {"entity_id": citizens[0]["id"], "type": "create_proposal",
         "params": {"target_id": gov["id"], "mutations": mutations,
                    "weight_model": "citizen", "title": "cap fiscal rate",
                    "proposal_type": "constitutional"}},
    ], headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    pid = r.json()[0]["proposal_id"]
    got = client.get(f"/admin/proposals/{pid}", headers=_auth("u-admin")).json()
    assert got["proposal_type"] == "constitutional"

    # all three citizens vote for -> unanimous, clears the 0.67 floor
    for ci in (0, 1, 2):
        client.post("/intents", json=[
            {"entity_id": citizens[ci]["id"], "type": "vote",
             "params": {"proposal_id": pid, "choice": "for"}},
        ], headers=_auth("u-admin"))

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "enact",
         "params": {"proposal_id": pid}},
    ], headers=_auth("u-admin"))
    assert r.json()[0]["proposal_status"] == "enacted"

    # the installed constitution now vetoes an over-cap fiscal policy
    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "set_fiscal_policy",
         "params": {"policy": '{"rate":"0.9"}'}},
    ], headers=_auth("u-admin"))
    assert r.json()[0]["status"] == "rejected"
    assert "cap" in r.json()[0]["reason"]


def test_ordinary_proposal_cannot_smuggle_a_validator_via_api(client):
    """The one-directional gate, over HTTP: an ordinary (simple-majority)
    proposal that tries to carry a set_validator mutation is rejected at
    propose — the constitution is unreachable from ordinary legislation."""
    gov = client.post("/admin/entities",
                      json={"name": "Polity", "entity_type": "government",
                            "owner_id": "u-admin"},
                      headers=_auth("u-admin")).json()
    citizen = client.post("/admin/entities",
                          json={"name": "A", "entity_type": "individual",
                                "owner_id": "u-admin"},
                          headers=_auth("u-admin")).json()
    mutations = json.dumps([
        {"type": "set_validator", "params": {"lineage_id": "cap", "source": "return false"}}
    ])
    r = client.post("/intents", json=[
        {"entity_id": citizen["id"], "type": "create_proposal",
         "params": {"target_id": gov["id"], "mutations": mutations,
                    "weight_model": "citizen"}},  # ordinary (default)
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "not allowed for ordinary" in result["reason"]


# ---------------------------------------------------------------------------
# shareholder governance (actors step 4c) — the HTTP path
# ---------------------------------------------------------------------------

def test_shareholders_enact_a_directive_via_api(client):
    """A corporation is a different row in the weight-model registry: the
    electorate is the holders of a symbol (the cap table), weighted by
    shares. Over POST /intents a shareholder proposes a behaviour-script
    directive for the firm, the majority shareholder carries it, and a
    non-holder cannot even vote — the same machinery as citizen democracy,
    reachable from the machine client."""
    firm = client.post("/admin/entities",
                       json={"name": "AcmeCorp", "entity_type": "business",
                             "owner_id": "u-admin"},
                       headers=_auth("u-admin")).json()
    # the firm may legislate its own behaviour script (the capability grant
    # that lets an enacted directive bind it — data, not new mechanism)
    client.patch(f"/admin/entities/{firm['id']}",
                 json={"capabilities": ["legislate"]},
                 headers=_auth("u-admin"))
    alice = client.post("/admin/entities",
                        json={"name": "Alice", "entity_type": "individual",
                              "owner_id": "u-admin"},
                        headers=_auth("u-admin")).json()
    bob = client.post("/admin/entities",
                      json={"name": "Bob", "entity_type": "individual",
                            "owner_id": "u-admin"},
                      headers=_auth("u-admin")).json()
    # the cap table: a 30/70 split
    for eid, qty in ((alice["id"], "30"), (bob["id"], "70")):
        client.post("/admin/holdings",
                    json={"entity_id": eid, "symbol": "ACME", "delta": qty},
                    headers=_auth("u-admin"))

    directive = json.dumps([{
        "type": "set_script",
        "params": {"script_type": "behaviour", "lineage_id": "strategy",
                   "source": "ctx.state.directed = 'by-shareholders'",
                   "entity_id": firm["id"]},
    }])
    # the 30% shareholder proposes; the weight model is share:ACME
    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "create_proposal",
         "params": {"target_id": firm["id"], "mutations": directive,
                    "weight_model": "share:ACME", "title": "pivot"}},
    ], headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    pid = r.json()[0]["proposal_id"]
    assert client.get(f"/admin/proposals/{pid}", headers=_auth("u-admin")).json()[
        "weight_model"] == "share:ACME"

    # a non-holder cannot vote
    outsider = client.post("/admin/entities",
                           json={"name": "Outsider", "entity_type": "individual",
                                 "owner_id": "u-admin"},
                           headers=_auth("u-admin")).json()
    r = client.post("/intents", json=[
        {"entity_id": outsider["id"], "type": "vote",
         "params": {"proposal_id": pid, "choice": "for"}},
    ], headers=_auth("u-admin"))
    assert r.json()[0]["status"] == "rejected"
    assert "electorate" in r.json()[0]["reason"]

    # the 70% shareholder carries it alone (a majority of cast share weight)
    client.post("/intents", json=[
        {"entity_id": bob["id"], "type": "vote",
         "params": {"proposal_id": pid, "choice": "for"}},
    ], headers=_auth("u-admin"))
    r = client.post("/intents", json=[
        {"entity_id": firm["id"], "type": "enact",
         "params": {"proposal_id": pid}},
    ], headers=_auth("u-admin"))
    assert r.json()[0]["proposal_status"] == "enacted"
    got = client.get(f"/admin/proposals/{pid}", headers=_auth("u-admin")).json()
    assert got["status"] == "enacted" and got["tally_yes"][:2] == "70"


# ---------------------------------------------------------------------------
# seize — expropriation of goods (actors step 2, goods/parcels half)
# ---------------------------------------------------------------------------

def test_seize_moves_goods_from_victim_to_state_via_api(client):
    """A seize-capable government expropriates goods from an individual over
    POST /intents — the goods/parcels analogue of levy, reachable from the
    machine client. A government without the capability is rejected."""
    gov = client.post("/admin/entities",
                      json={"name": "State", "entity_type": "government",
                            "owner_id": "u-admin"},
                      headers=_auth("u-admin")).json()
    client.patch(f"/admin/entities/{gov['id']}",
                 json={"capabilities": ["seize"]},
                 headers=_auth("u-admin"))
    victim = client.post("/admin/entities",
                         json={"name": "Victim", "entity_type": "individual",
                               "owner_id": "u-admin"},
                         headers=_auth("u-admin")).json()
    client.post("/admin/holdings",
                json={"entity_id": victim["id"], "symbol": "GRAIN", "delta": "1000"},
                headers=_auth("u-admin"))

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "seize",
         "params": {"from_entity_id": victim["id"], "symbol": "GRAIN",
                    "quantity": "300", "rule_ref": "tax:inkind"}},
    ], headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()[0]["status"] == "applied"
    assert r.json()[0]["seized_goods"] == "300"

    victim_holdings = {h["symbol"]: h["quantity"] for h in
                       client.get(f"/admin/holdings?entity_id={victim['id']}",
                                  headers=_auth("u-admin")).json()}
    gov_holdings = {h["symbol"]: h["quantity"] for h in
                    client.get(f"/admin/holdings?entity_id={gov['id']}",
                               headers=_auth("u-admin")).json()}
    assert victim_holdings.get("GRAIN", "0").startswith("700")
    assert gov_holdings.get("GRAIN", "0").startswith("300")

    # a government without the seize capability cannot expropriate
    plain = client.post("/admin/entities",
                        json={"name": "PlainGov", "entity_type": "government",
                              "owner_id": "u-admin"},
                        headers=_auth("u-admin")).json()
    r = client.post("/intents", json=[
        {"entity_id": plain["id"], "type": "seize",
         "params": {"from_entity_id": victim["id"], "symbol": "GRAIN",
                    "quantity": "1", "rule_ref": "r"}},
    ], headers=_auth("u-admin"))
    assert r.json()[0]["status"] == "rejected"
    assert "seize" in r.json()[0]["reason"]
