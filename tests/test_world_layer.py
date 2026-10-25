"""World layer: deterministic tile2d scenes plus W2 walk/talk presence."""
import time
from decimal import Decimal

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api import events
from econ.api.auth import create_token
from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econ.world.presence import PresenceError, runtime
from econ.world.scene import project_scene
from econ.world.tile2d import CONFIG_KEY
from econengine import edges, places
from econengine.tick import run_tick
from econengine.models import (
    Base, Deposit, Entity, EntityStatus, EntityType, Facility, Parcel, Place,
    Process, ProcessStatus, Recipe, SpatialEdge, Tick, User, WorldSetting,
)


@pytest.fixture
def world_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app.state._test_engine = engine
    runtime.reset()

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
            User(id="u-spectator", email="watch@x", name="Watcher",
                 provider="test", provider_id="1"),
            User(id="u-owner", email="owner@x", name="Owner",
                 provider="test", provider_id="2"),
        ])
        session.commit()
    try:
        yield TestClient(app), engine
    finally:
        app.dependency_overrides.clear()
        if hasattr(app.state, "_test_engine"):
            del app.state._test_engine
        runtime.reset()


def _auth(user_id: str = "u-spectator") -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def test_stone_age_pack_authors_its_valley_geometry():
    """The content pack joins engine places to tile2d via opaque extent refs."""
    from experiments.world.stone_age import create_content

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        create_content(session, verify=False)
        scene = project_scene(session)

    places_by_key = {place["key"]: place for place in scene["places"]}
    assert places_by_key["HEARTH"]["position"] == {"x": 38.0, "y": 52.0}
    assert places_by_key["THICKET"]["position"] == {"x": 28.0, "y": 36.0}
    assert places_by_key["RIVER"]["position"] == {"x": 22.0, "y": 60.0}
    assert places_by_key["RIDGE_PASS"]["position"] == {"x": 70.0, "y": 30.0}
    assert len(scene["roads"]) == 13
    assert {actor["name"] for actor in scene["actors"]} == {
        "Trading Post", "Wolf Pack I", "Wolf Pack II",
    }
    assert scene["revision"].startswith("0:")


def test_bootstrap_requires_no_entity_and_chunks_are_deterministic(world_client):
    client, _ = world_client
    response = client.get("/world/bootstrap", headers=_auth())
    assert response.status_code == 200, response.text
    boot = response.json()
    assert boot["protocol"] == "econ-world.v1"
    assert boot["session"] == {
        "role": "spectator", "user_id": "u-spectator",
        "entity_id": None, "capabilities": [],
    }
    assert boot["geometry"]["id"] == "tile2d.v1"
    assert boot["scene"]["changed"] is True
    assert boot["scene"]["actors"] == []
    assert boot["assets"]["actor"]["animations"]["travel"]["frames"] == [4, 5, 6, 7]

    template = boot["endpoints"]["chunks"]
    url = template.replace("{cx}", "0").replace("{cy}", "0")
    first = client.get(url, headers=_auth())
    second = client.get(url, headers=_auth("u-owner"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()  # two clients receive identical terrain
    assert client.get("/world/scene", headers=_auth()).json() == client.get(
        "/world/scene", headers=_auth("u-owner")).json()
    chunk = first.json()
    size = boot["geometry"]["chunk_size"]
    assert len(chunk["tiles"]) == size * size
    valid_tiles = {tile["id"] for tile in boot["assets"]["tiles"]}
    assert set(chunk["tiles"]) <= valid_tiles
    assert "immutable" in first.headers["cache-control"]

    unchanged = client.get(
        "/world/scene", params={"since": boot["scene"]["revision"]}, headers=_auth())
    assert unchanged.status_code == 200
    assert unchanged.json() == {
        "changed": False, "revision": boot["scene"]["revision"], "tick": 0,
    }
    assert client.get("/world/chunks/999/0", headers=_auth()).status_code == 404


def test_world_stream_sends_hello_then_scene_deltas(world_client):
    client, engine = world_client
    with Session(engine) as session:
        hearth = places.create_place(session, "HEARTH", "HEARTH")
        forest = places.create_place(session, "FOREST", "FOREST")
        session.add(Entity(
            id="e-watched", name="Watched", entity_type=EntityType.INDIVIDUAL,
            location_place_id=hearth.id))
        session.commit()

    token = create_token("u-spectator", "watch@x", False)
    with client.websocket_connect("/world/stream") as socket:
        socket.send_json({"type": "auth", "token": token})
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["bootstrap"]["session"]["role"] == "spectator"
        assert hello["bootstrap"]["endpoints"]["stream"] == "/world/stream"
        before = hello["bootstrap"]["scene"]
        assert before["actors"][0]["place"] == "HEARTH"

        with Session(engine) as session:
            places.move_entity(session, session.get(Entity, "e-watched"), "FOREST")
            session.commit()
        events.publish("tick", {"tick": 0, "events": []})
        delta = socket.receive_json()

    assert delta["type"] == "scene_delta"
    assert delta["base_revision"] == before["revision"]
    assert delta["revision"] != before["revision"]
    assert delta["actors"][0]["place"] == "FOREST"
    assert "roads" not in delta
    assert "places" not in delta
    assert "objects" not in delta  # unchanged collections are omitted


def test_world_stream_rejects_a_spectator_without_a_user(world_client):
    client, _ = world_client
    token = create_token("u-missing", "missing@x", False)
    with client.websocket_connect("/world/stream") as socket:
        socket.send_json({"type": "auth", "token": token})
        message = socket.receive_json()
    assert message == {"type": "error", "reason": "unauthorized"}


def test_generator_seed_is_versioned_and_invalid_config_fails_closed(world_client):
    client, engine = world_client
    boot = client.get("/world/bootstrap", headers=_auth()).json()
    url = boot["endpoints"]["chunks"].replace("{cx}", "1").replace("{cy}", "1")
    before = client.get(url, headers=_auth()).json()["tiles"]

    with Session(engine) as session:
        session.add(WorldSetting(key=CONFIG_KEY, value={"seed": "another-valley"}))
        session.commit()
    changed_boot = client.get("/world/bootstrap", headers=_auth()).json()
    assert changed_boot["geometry"]["cache_key"] != boot["geometry"]["cache_key"]
    changed_url = changed_boot["endpoints"]["chunks"].replace("{cx}", "1").replace("{cy}", "1")
    after = client.get(changed_url, headers=_auth()).json()["tiles"]
    assert after != before

    with Session(engine) as session:
        setting = session.get(WorldSetting, CONFIG_KEY)
        setting.value = {"chunk_size": 0}
        session.commit()
    response = client.get("/world/bootstrap", headers=_auth())
    assert response.status_code == 503
    assert "chunk_size" in response.json()["detail"]


def test_scene_projects_physical_public_state_without_private_process_detail(world_client):
    client, engine = world_client
    with Session(engine) as session:
        hearth = places.create_place(
            session, "HEARTH", "HEARTH", name="Hearth",
            extent_ref="tile2d:v1:10,12")
        forest = places.create_place(
            session, "FOREST", "FOREST", name="Forest",
            extent_ref="tile2d:v1:20,12")
        road = edges.create_edge(session, hearth, forest, "WALK", 2)
        worker = Entity(
            id="e-worker", name="Aster", entity_type=EntityType.INDIVIDUAL,
            owner_id="u-owner", location_place_id=hearth.id)
        traveller = Entity(
            id="e-traveller", name="Briar", entity_type=EntityType.INDIVIDUAL,
            location_place_id=hearth.id)
        abstract = Entity(
            id="e-abstract", name="Off map", entity_type=EntityType.BUSINESS)
        session.add_all([worker, traveller, abstract])
        work_recipe = Recipe(code="SECRET_RECIPE", name="Private work", duration_ticks=2)
        travel_recipe = Recipe(code="TRAVEL_WALK", name="Walk", duration_ticks=1)
        session.add_all([work_recipe, travel_recipe])
        session.flush()
        session.add_all([
            Process(
                id="p-work", recipe_id=work_recipe.id, entity_id=worker.id,
                started_tick=1, completes_tick=3, status=ProcessStatus.RUNNING),
            Process(
                id="p-travel", recipe_id=travel_recipe.id, entity_id=traveller.id,
                started_tick=1, completes_tick=3, status=ProcessStatus.RUNNING,
                is_travel=True, edge_id=road.id),
        ])
        parcel = Parcel(
            id="land-1", name="Commons", parcel_type="COMMONS",
            place_id=hearth.id)
        session.add(parcel)
        session.flush()
        session.add_all([
            Facility(
                id="fire-1", parcel_id=parcel.id, facility_type="FIRE",
                fuel=Decimal("2"), fuel_capacity=Decimal("6")),
            Deposit(
                id="berries-1", parcel_id=parcel.id, symbol="BERRIES",
                quantity=Decimal("4"), capacity=Decimal("4")),
            Tick(number=1, events=[]),
            Tick(number=2, events=[
                {"type": "say", "entity_id": worker.id, "status": "applied",
                 "params": {"text": "hello, valley"}},
                {"type": "say", "entity_id": worker.id, "status": "rejected",
                 "params": {"text": "not spoken"}},
                {"type": "start_process", "entity_id": worker.id,
                 "status": "applied", "params": {"recipe": "SECRET_RECIPE"}},
            ]),
        ])
        session.commit()

    response = client.get("/world/scene", headers=_auth())
    assert response.status_code == 200, response.text
    scene = response.json()
    assert scene["tick"] == 2 and scene["clock"]["tick"] == 2
    actors = {actor["name"]: actor for actor in scene["actors"]}
    assert actors["Aster"]["action"] == "work"
    assert actors["Briar"]["action"] == "travel"
    assert actors["Briar"]["action_progress"] == 0.5
    assert actors["Briar"]["position"] == {"x": 15.0, "y": 12.0}
    assert scene["unplaced_actor_count"] == 1
    assert all("owner_id" not in actor for actor in scene["actors"])
    assert "SECRET_RECIPE" not in response.text
    assert "not spoken" not in response.text
    assert [row["text"] for row in scene["speech"]] == ["hello, valley"]

    objects = {(obj["kind"], obj["id"]): obj for obj in scene["objects"]}
    assert objects[("facility", "fire-1")]["lit"] is True
    assert objects[("deposit", "berries-1")]["symbol"] == "BERRIES"
    assert "quantity" not in objects[("deposit", "berries-1")]
    assert objects[("parcel", "land-1")]["place"] == "HEARTH"

    old_revision = scene["revision"]
    with Session(engine) as session:
        places.move_entity(session, session.get(Entity, "e-worker"), "FOREST")
        session.commit()
    moved = client.get(
        "/world/scene", params={"since": old_revision}, headers=_auth()).json()
    assert moved["changed"] is True and moved["revision"] != old_revision
    assert {a["name"]: a for a in moved["actors"]}["Aster"]["place"] == "FOREST"


def test_world_stream_possesses_moves_speaks_and_releases(world_client, monkeypatch):
    monkeypatch.setenv("ECON_WORLD_TICK_SECONDS", "60")
    client, engine = world_client
    with Session(engine) as session:
        hearth = places.create_place(
            session, "HEARTH", "HEARTH", extent_ref="tile2d:v1:10,10")
        session.add_all([
            Entity(
                id="e-player", name="Player One",
                entity_type=EntityType.INDIVIDUAL,
                owner_id="u-owner", location_place_id=hearth.id),
            Entity(
                id="e-firm", name="Player Firm",
                entity_type=EntityType.BUSINESS,
                owner_id="u-owner", location_place_id=hearth.id),
            Entity(
                id="e-fixed", name="Fixed Founder",
                entity_type=EntityType.INDIVIDUAL,
                owner_id="u-owner", location_place_id=hearth.id,
                is_fixed=True),
        ])
        session.commit()

    token = create_token("u-owner", "owner@x", False)
    with client.websocket_connect("/world/stream") as socket:
        socket.send_json({"type": "auth", "token": token})
        hello = socket.receive_json()
        assert hello["bootstrap"]["player_entities"] == [{
            "id": "e-player", "name": "Player One", "place": "HEARTH",
        }]
        start = next(
            actor["position"] for actor in hello["bootstrap"]["scene"]["actors"]
            if actor["id"] == "e-player"
        )

        socket.send_json({
            "type": "possess", "entity_id": "e-firm", "seq": 1,
        })
        refused = socket.receive_json()
        assert refused["status"] == "rejected"
        assert refused["reason"] == "only an individual can be possessed"
        socket.send_json({
            "type": "possess", "entity_id": "e-fixed", "seq": 1,
        })
        refused = socket.receive_json()
        assert refused["status"] == "rejected"
        assert refused["reason"] == "fixed world entity cannot be possessed"

        socket.send_json({
            "type": "possess", "entity_id": "e-player", "seq": 1,
        })
        possessed = socket.receive_json()
        assert possessed["type"] == "command_result"
        assert possessed["status"] == "accepted"
        assert possessed["actor"]["action"] == "walk"
        delta = socket.receive_json()
        assert delta["type"] == "presence_delta"
        assert delta["actors"][0]["control"] == "player"

        socket.send_json({
            "type": "move", "entity_id": "e-player", "seq": 2,
            "dx": 1, "dy": 0,
        })
        moved = socket.receive_json()
        assert moved["status"] == "accepted"
        assert moved["actor"]["velocity"] == {"x": 1.0, "y": 0.0}
        delta = socket.receive_json()
        assert delta["actors"][0]["velocity"] == {"x": 1.0, "y": 0.0}

        time.sleep(0.08)
        scene = client.get("/world/scene", headers=_auth("u-owner")).json()
        actor = next(row for row in scene["actors"] if row["id"] == "e-player")
        assert actor["action"] == "walk" and actor["control"] == "player"
        assert actor["position"]["x"] > start["x"]
        assert actor["velocity"] == {"x": 1.0, "y": 0.0}

        socket.send_json({
            "type": "say", "entity_id": "e-player", "seq": 3,
            "text": "hello from the valley",
        })
        spoken = socket.receive_json()
        assert spoken["type"] == "command_result"
        assert spoken["command"] == "say" and spoken["status"] == "queued"

        with Session(engine) as session:
            run_tick(session)
            session.commit()
        events.publish("tick", {"tick": 1, "events": []})
        speech_delta = socket.receive_json()
        assert speech_delta["type"] == "scene_delta"
        assert speech_delta["speech"][-1]["text"] == "hello from the valley"

        socket.send_json({
            "type": "release", "entity_id": "e-player", "seq": 4,
        })
        released = socket.receive_json()
        assert released["status"] == "accepted"
        delta = socket.receive_json()
        assert delta["type"] == "presence_delta"
        assert delta["released"] == ["e-player"]

    scene = client.get("/world/scene", headers=_auth("u-owner")).json()
    actor = next(row for row in scene["actors"] if row["id"] == "e-player")
    assert actor["action"] == "idle"
    assert "control" not in actor and "velocity" not in actor


def test_world_stream_refuses_spectator_writes_and_double_possession(world_client):
    client, engine = world_client
    with Session(engine) as session:
        hearth = places.create_place(session, "HEARTH", "HEARTH")
        session.add(Entity(
            id="e-owned", name="Owned", entity_type=EntityType.INDIVIDUAL,
            owner_id="u-owner", location_place_id=hearth.id))
        session.commit()

    spectator_token = create_token("u-spectator", "watch@x", False)
    with client.websocket_connect("/world/stream") as socket:
        socket.send_json({"type": "auth", "token": spectator_token})
        hello = socket.receive_json()
        assert hello["bootstrap"]["player_entities"] == []
        socket.send_json({
            "type": "possess", "entity_id": "e-owned", "seq": 1,
        })
        assert socket.receive_json()["reason"] == "entity not found"
        socket.send_json({
            "type": "move", "entity_id": "e-owned", "seq": 2,
            "dx": 1, "dy": 0,
        })
        refused = socket.receive_json()
        assert refused["status"] == "rejected"
        assert refused["reason"] == "entity is not controlled by this session"
        socket.send_json({
            "type": "say", "entity_id": "e-owned", "seq": 3, "text": "forged",
        })
        assert socket.receive_json()["status"] == "rejected"
        for seq in range(4, 21):
            socket.send_json({"type": "unknown", "seq": seq})
            assert socket.receive_json()["reason"] == "unknown command"
        socket.send_json({"type": "unknown", "seq": 21})
        assert socket.receive_json()["reason"] == "command rate limit exceeded"

    owner_token = create_token("u-owner", "owner@x", False)
    with client.websocket_connect("/world/stream") as first:
        first.send_json({"type": "auth", "token": owner_token})
        first.receive_json()
        first.send_json({"type": "possess", "entity_id": "e-owned", "seq": 1})
        assert first.receive_json()["status"] == "accepted"
        first.receive_json()  # presence_delta
        with client.websocket_connect("/world/stream") as second:
            second.send_json({"type": "auth", "token": owner_token})
            second.receive_json()
            second.send_json({
                "type": "possess", "entity_id": "e-owned", "seq": 1,
            })
            refused = second.receive_json()
    assert refused["status"] == "rejected"
    assert refused["reason"] == "entity is already controlled"


def test_world_stream_say_requires_an_armed_world_clock(world_client):
    client, engine = world_client
    with Session(engine) as session:
        hearth = places.create_place(session, "HEARTH", "HEARTH")
        session.add(Entity(
            id="e-speaker", name="Speaker", entity_type=EntityType.INDIVIDUAL,
            owner_id="u-owner", location_place_id=hearth.id))
        session.commit()

    token = create_token("u-owner", "owner@x", False)
    with client.websocket_connect("/world/stream") as socket:
        socket.send_json({"type": "auth", "token": token})
        socket.receive_json()
        socket.send_json({"type": "possess", "entity_id": "e-speaker", "seq": 1})
        assert socket.receive_json()["status"] == "accepted"
        socket.receive_json()  # presence_delta
        socket.send_json({
            "type": "say", "entity_id": "e-speaker", "seq": 2,
            "text": "can anyone hear this?",
        })
        refused = socket.receive_json()
    assert refused["status"] == "rejected"
    assert refused["reason"] == "say requires an armed world clock"


def test_world_stream_broadcasts_player_presence_to_a_spectator(world_client):
    client, engine = world_client
    with Session(engine) as session:
        hearth = places.create_place(session, "HEARTH", "HEARTH")
        session.add(Entity(
            id="e-seen", name="Seen", entity_type=EntityType.INDIVIDUAL,
            owner_id="u-owner", location_place_id=hearth.id))
        session.commit()

    owner_token = create_token("u-owner", "owner@x", False)
    spectator_token = create_token("u-spectator", "watch@x", False)
    with client.websocket_connect("/world/stream") as spectator:
        spectator.send_json({"type": "auth", "token": spectator_token})
        assert spectator.receive_json()["type"] == "hello"
        with client.websocket_connect("/world/stream") as owner:
            owner.send_json({"type": "auth", "token": owner_token})
            owner.receive_json()
            owner.send_json({"type": "possess", "entity_id": "e-seen", "seq": 1})
            assert owner.receive_json()["status"] == "accepted"
            assert owner.receive_json()["type"] == "presence_delta"
            seen = spectator.receive_json()
            assert seen["type"] == "presence_delta"
            assert seen["actors"][0]["id"] == "e-seen"

            owner.send_json({
                "type": "move", "entity_id": "e-seen", "seq": 2,
                "dx": 0, "dy": -1,
            })
            assert owner.receive_json()["status"] == "accepted"
            owner.receive_json()  # presence_delta
            moved = spectator.receive_json()

    assert moved["type"] == "presence_delta"
    assert moved["actors"][0]["velocity"] == {"x": 0.0, "y": -1.0}


def test_world_stream_engine_process_refuses_and_reclaims_possession(world_client):
    client, engine = world_client
    with Session(engine) as session:
        hearth = places.create_place(session, "HEARTH", "HEARTH")
        busy = Entity(
            id="e-busy", name="Busy", entity_type=EntityType.INDIVIDUAL,
            owner_id="u-owner", location_place_id=hearth.id)
        walker = Entity(
            id="e-walker", name="Walker", entity_type=EntityType.INDIVIDUAL,
            owner_id="u-owner", location_place_id=hearth.id)
        recipe = Recipe(code="W2_WORK", name="W2 work", duration_ticks=2)
        session.add_all([busy, walker, recipe])
        session.flush()
        session.add(Process(
            recipe_id=recipe.id, entity_id=busy.id,
            started_tick=0, completes_tick=2, status=ProcessStatus.RUNNING))
        session.commit()

    token = create_token("u-owner", "owner@x", False)
    with client.websocket_connect("/world/stream") as socket:
        socket.send_json({"type": "auth", "token": token})
        socket.receive_json()
        socket.send_json({"type": "possess", "entity_id": "e-busy", "seq": 1})
        refused = socket.receive_json()
        assert refused["status"] == "rejected"
        assert refused["reason"] == "entity is busy with an engine process"

        socket.send_json({"type": "possess", "entity_id": "e-walker", "seq": 2})
        assert socket.receive_json()["status"] == "accepted"
        assert socket.receive_json()["type"] == "presence_delta"

        with Session(engine) as session:
            recipe = session.query(Recipe).filter_by(code="W2_WORK").one()
            session.add(Process(
                recipe_id=recipe.id, entity_id="e-walker",
                started_tick=0, completes_tick=2, status=ProcessStatus.RUNNING))
            session.commit()
        events.publish("tick", {"tick": 0, "events": []})
        scene_delta = socket.receive_json()
        presence_delta = socket.receive_json()

    assert scene_delta["type"] == "scene_delta"
    assert {row["id"]: row for row in scene_delta["actors"]}[
        "e-walker"]["action"] == "work"
    assert presence_delta["type"] == "presence_delta"
    assert presence_delta["released"] == ["e-walker"]
    assert presence_delta["reason"] == "engine_process"


def test_presence_runtime_validates_sequence_rate_bounds_and_collisions():
    runtime.reset()
    try:
        runtime.attach(
            connection_id="c1", entity_id="e1", owner_id="u1",
            place="HEARTH", position={"x": 1.0, "y": 1.0},
            bounds=(1.0, 1.0, 3.0, 3.0),
            obstacles=[{"x": 1.5, "y": 1.0, "radius": 0.5}],
        )
        # The natural push would leave the walk box, so the resolver slides
        # the actor to the obstacle's other side.
        assert runtime.snapshot()["e1"]["position"]["x"] >= 2.319
        runtime.move(connection_id="c1", entity_id="e1", seq=1, dx=0, dy=0)
        with pytest.raises(PresenceError, match="stale command sequence"):
            runtime.move(
                connection_id="c1", entity_id="e1", seq=1, dx=1, dy=0)
        with pytest.raises(PresenceError, match="normalized"):
            runtime.move(
                connection_id="c1", entity_id="e1", seq=99, dx=2, dy=0)
        for seq in range(2, 21):
            runtime.move(
                connection_id="c1", entity_id="e1", seq=seq, dx=0, dy=0)
        with pytest.raises(PresenceError, match="rate limit"):
            runtime.move(
                connection_id="c1", entity_id="e1", seq=21, dx=0, dy=0)

        runtime.reset()
        runtime.attach(
            connection_id="c1", entity_id="e1", owner_id="u1",
            place="HEARTH", position={"x": 2.9, "y": 2.0},
            bounds=(1.0, 1.0, 3.0, 3.0),
        )
        runtime.move(connection_id="c1", entity_id="e1", seq=1, dx=1, dy=0)
        time.sleep(0.08)
        overlay = runtime.snapshot()["e1"]
        assert overlay["position"]["x"] == 3.0
        assert overlay["position"]["y"] == 2.0
    finally:
        runtime.reset()
