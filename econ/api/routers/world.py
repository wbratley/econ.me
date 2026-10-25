"""World-layer API (docs/world-layer.md).

W1 supplies authenticated, entity-free spectator bootstrap/chunk/scene reads
plus a read-only live stream. W2 extends that stream with possession and a
small command vocabulary (`possess`, `release`, `move`, `say`). Local
presence is process-local world state; economic location and speech still go
through the engine's ordinary places/travel/intent paths.
"""
import asyncio
import time
import uuid
from collections import deque

from fastapi import (
    APIRouter, Depends, HTTPException, Query, Response, WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session

from econ.api import events
from econ.api.auth import decode_token
from econ.api.deps import get_current_user, get_session
from econ.api.intents import submit_intent
from econ.api.rounds import world_tick_seconds
from econ.db import engine as default_engine
from econ.world import scene as scene_mod
from econ.world.presence import PresenceError, runtime
from econ.world.tile2d import WorldConfigError, generate_chunk, load_config
from econengine.models import Entity, EntityStatus, EntityType, User

router = APIRouter(prefix="/world", tags=["world-layer"])


def _config_error(exc: WorldConfigError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Invalid world-layer configuration: {exc}")


@router.get("/bootstrap")
def world_bootstrap(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Protocol, geometry, art bindings, possessable entities, snapshot."""
    try:
        return scene_mod.bootstrap(session, current_user)
    except WorldConfigError as exc:
        raise _config_error(exc) from exc


@router.get("/scene")
def world_scene(
    since: str | None = Query(default=None, max_length=80),
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A full scene, or ``changed:false`` when ``since`` is still current."""
    try:
        return scene_mod.scene_response(session, since)
    except WorldConfigError as exc:
        raise _config_error(exc) from exc


@router.get("/chunks/{cx}/{cy}")
def world_chunk(
    cx: int,
    cy: int,
    response: Response,
    v: str | None = Query(default=None, max_length=64),
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """One deterministic row-major terrain chunk; never an economic fact."""
    try:
        config = load_config(session)
        chunk = generate_chunk(config, cx, cy)
    except WorldConfigError as exc:
        raise _config_error(exc) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Chunk outside world bounds") from exc
    if v == config.cache_key:
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        response.headers["ETag"] = f'"{chunk["revision"]}"'
    else:
        response.headers["Cache-Control"] = "private, no-cache"
    return chunk


# ---------------------------------------------------------------------------
# Live channel: W1 spectator deltas + W2 possession commands
# ---------------------------------------------------------------------------


def _engine_for(websocket: WebSocket):
    """Test fixtures override app.state; production rides the configured db."""
    return getattr(websocket.app.state, "_test_engine", None) or default_engine


def _bootstrap_for_token(engine, token: str) -> dict | None:
    payload = decode_token(token)
    user_id = payload.get("sub") if payload else None
    if not isinstance(user_id, str):
        return None
    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None:
            return None
        return scene_mod.bootstrap(session, user)


def _scene_for(engine) -> dict:
    with Session(engine) as session:
        return scene_mod.project_scene(session)


def _local_bounds(place: dict, world: dict) -> tuple[float, float, float, float]:
    """W2 walking bounds: a small square around the semantic place anchor."""
    radius = 4.0
    return (
        max(world["x"] + 0.5, place["position"]["x"] - radius),
        max(world["y"] + 0.5, place["position"]["y"] - radius),
        min(world["x"] + world["width"] - 0.5, place["position"]["x"] + radius),
        min(world["y"] + world["height"] - 0.5, place["position"]["y"] + radius),
    )


def _attach_for(engine, user_id: str, connection_id: str, entity_id: str) -> dict:
    """Prove platform ownership and hand local presence to the world seat."""
    with Session(engine) as session:
        entity = session.get(Entity, entity_id)
        if entity is None or entity.owner_id != user_id:
            raise PresenceError("entity not found")
        if entity.status != EntityStatus.ACTIVE:
            raise PresenceError("entity is incapacitated")
        if entity.entity_type != EntityType.INDIVIDUAL:
            raise PresenceError("only an individual can be possessed")
        if entity.is_fixed:
            raise PresenceError("fixed world entity cannot be possessed")
        if entity.location_place_id is None:
            raise PresenceError("entity is not placed in the world")
        if runtime.is_controlled(entity_id):
            raise PresenceError("entity is already controlled")
        scene = scene_mod.project_scene(session)

    actor = next((row for row in scene["actors"] if row["id"] == entity_id), None)
    if actor is None:
        raise PresenceError("entity is not visible in this geometry")
    if actor["action"] != "idle":
        raise PresenceError("entity is busy with an engine process")
    place = next(row for row in scene["places"] if row["key"] == actor["place"])
    bounds = _local_bounds(place, scene["world"])
    obstacles = [
        {"x": row["position"]["x"], "y": row["position"]["y"],
         "radius": 0.55 if row["kind"] == "facility" else 0.42}
        for row in scene["objects"]
        if row["place"] == actor["place"] and row["kind"] in {"facility", "deposit"}
    ]
    obstacles.extend(
        {"x": row["position"]["x"], "y": row["position"]["y"], "radius": 0.32}
        for row in scene["actors"]
        if row["place"] == actor["place"] and row["id"] != entity_id
    )
    return runtime.attach(
        connection_id=connection_id,
        entity_id=entity_id,
        owner_id=user_id,
        place=actor["place"],
        position=actor["position"],
        bounds=bounds,
        obstacles=obstacles,
    )


def _say_for(engine, user_id: str, entity_id: str, text: str) -> dict:
    """Speak through the one intent surface; never a parallel chat channel."""
    if not isinstance(text, str) or not text.strip() or len(text) > 256:
        raise PresenceError("say text must be 1-256 characters")
    if world_tick_seconds() <= 0:
        raise PresenceError("say requires an armed world clock")
    with Session(engine) as session:
        entity = session.get(Entity, entity_id)
        if entity is None or entity.owner_id != user_id:
            raise PresenceError("entity not found")
        result = submit_intent(session, entity, "say", {"text": text.strip()})
    if result["status"] == "rejected":
        raise PresenceError(result.get("reason") or "say rejected")
    return result


def _command_result(
    command: str, status: str, *, entity_id: str | None = None,
    seq: int | None = None, reason: str | None = None, **extra,
) -> dict:
    result = {"type": "command_result", "command": command, "status": status}
    if entity_id is not None:
        result["entity_id"] = entity_id
    if seq is not None:
        result["seq"] = seq
    if reason is not None:
        result["reason"] = reason
    result.update(extra)
    return result


async def _handle_world_command(
    websocket: WebSocket, engine, user_id: str, connection_id: str,
    message: dict, command_times: deque[float],
) -> None:
    command = message.get("type")
    entity_id = message.get("entity_id")
    seq = message.get("seq")
    entity_id = entity_id if isinstance(entity_id, str) else None
    seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None

    try:
        now = time.monotonic()
        while command_times and now - command_times[0] >= 1.0:
            command_times.popleft()
        if len(command_times) >= 20:
            raise PresenceError("command rate limit exceeded")
        command_times.append(now)

        if command == "possess":
            if entity_id is None:
                raise PresenceError("entity_id is required")
            actor = await asyncio.to_thread(
                _attach_for, engine, user_id, connection_id, entity_id)
            await websocket.send_json(_command_result(
                "possess", "accepted", entity_id=entity_id, seq=seq,
                actor=actor,
            ))
        elif command == "release":
            if entity_id is None or seq is None:
                raise PresenceError("entity_id and seq are required")
            released = runtime.release(
                connection_id=connection_id, entity_id=entity_id, seq=seq)
            await websocket.send_json(_command_result(
                "release", "accepted", entity_id=released, seq=seq))
        elif command == "move":
            if entity_id is None or seq is None:
                raise PresenceError("entity_id and seq are required")
            actor = runtime.move(
                connection_id=connection_id, entity_id=entity_id, seq=seq,
                dx=message.get("dx", 0), dy=message.get("dy", 0),
            )
            await websocket.send_json(_command_result(
                "move", "accepted", entity_id=entity_id, seq=seq,
                actor=actor,
            ))
        elif command == "say":
            if entity_id is None or seq is None:
                raise PresenceError("entity_id and seq are required")
            presence = runtime.authorize(
                connection_id=connection_id, entity_id=entity_id, seq=seq)
            if presence.owner_id != user_id:
                raise PresenceError("entity not found")
            result = await asyncio.to_thread(
                _say_for, engine, user_id, entity_id, message.get("text", ""))
            await websocket.send_json(_command_result(
                "say", result["status"], entity_id=entity_id, seq=seq,
                reason=result.get("reason"),
            ))
        else:
            raise PresenceError("unknown command")
    except PresenceError as exc:
        await websocket.send_json(_command_result(
            str(command or "unknown"), "rejected", entity_id=entity_id,
            seq=seq, reason=exc.reason,
        ))


async def _stream_loop(
    websocket: WebSocket, engine, user_id: str, connection_id: str,
    bootstrap: dict,
) -> None:
    queue = events.subscribe()
    presence_queue = runtime.subscribe()
    current = await asyncio.to_thread(_scene_for, engine)
    bootstrap["scene"] = current
    await websocket.send_json({"type": "hello", "bootstrap": bootstrap})

    command_times: deque[float] = deque()
    tasks: dict[asyncio.Task, str] = {
        asyncio.create_task(queue.get()): "engine",
        asyncio.create_task(presence_queue.get()): "presence",
        asyncio.create_task(websocket.receive_json()): "client",
    }
    try:
        while True:
            done, _pending = await asyncio.wait(
                tasks.keys(), timeout=15.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await websocket.send_json({
                    "type": "ping", "tick": current["tick"],
                    "revision": current["revision"],
                })
                continue
            for task in done:
                kind = tasks.pop(task)
                if kind == "client":
                    try:
                        message = task.result()
                    except WebSocketDisconnect:
                        raise
                    except ValueError:
                        await websocket.send_json(_command_result(
                            "unknown", "rejected", reason="invalid JSON message"))
                    else:
                        if not isinstance(message, dict):
                            await websocket.send_json(_command_result(
                                "unknown", "rejected", reason="message must be an object"))
                        else:
                            await _handle_world_command(
                                websocket, engine, user_id, connection_id,
                                message, command_times,
                            )
                    tasks[asyncio.create_task(websocket.receive_json())] = "client"
                elif kind == "presence":
                    await websocket.send_json(task.result())
                    tasks[asyncio.create_task(presence_queue.get())] = "presence"
                else:
                    event, _data = task.result()
                    tasks[asyncio.create_task(queue.get())] = "engine"
                    if event not in {"tick", "round_closed"}:
                        continue
                    try:
                        latest = await asyncio.to_thread(_scene_for, engine)
                    except WorldConfigError as exc:
                        await websocket.send_json({
                            "type": "error", "reason": "invalid_world_config",
                            "detail": str(exc),
                        })
                        await websocket.close(code=1011)
                        return
                    released = runtime.reconcile_scene(latest)
                    if released:
                        latest = await asyncio.to_thread(_scene_for, engine)
                    if latest["revision"] != current["revision"]:
                        await websocket.send_json(scene_mod.scene_delta(current, latest))
                        current = latest
    finally:
        for task in tasks:
            task.cancel()
        events.unsubscribe(queue)
        runtime.unsubscribe(presence_queue)
        runtime.detach_connection(connection_id)


@router.websocket("/stream")
async def world_stream(websocket: WebSocket) -> None:
    """Snapshot/deltas for spectators plus owned-entity player commands.

    Browser clients authenticate with a first ``{"type":"auth","token":...}``
    message (tokens never enter the URL); non-browser clients may use the
    ordinary Authorization header instead. Spectators that send a command get
    a refusal; only an account-owned, ACTIVE, placed, idle entity can be
    possessed.
    """
    header = websocket.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    await websocket.accept()
    if token is None:
        try:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
            await websocket.close(code=4401)
            return
        if not isinstance(message, dict) or message.get("type") != "auth":
            await websocket.close(code=4400)
            return
        raw_token = message.get("token")
        token = raw_token if isinstance(raw_token, str) else ""

    engine = _engine_for(websocket)
    try:
        bootstrap = await asyncio.to_thread(_bootstrap_for_token, engine, token)
    except WorldConfigError as exc:
        await websocket.send_json({
            "type": "error", "reason": "invalid_world_config", "detail": str(exc),
        })
        await websocket.close(code=1011)
        return
    if bootstrap is None:
        await websocket.send_json({"type": "error", "reason": "unauthorized"})
        await websocket.close(code=4401)
        return

    user_id = bootstrap["session"]["user_id"]
    connection_id = uuid.uuid4().hex
    try:
        await _stream_loop(websocket, engine, user_id, connection_id, bootstrap)
    except WebSocketDisconnect:
        return
