"""In-memory W2 presence: player-controlled local movement.

This module is deliberately outside ``econengine``. It owns only cosmetic,
world-layer facts: where a possessed actor stands within a place and which
way it is currently walking. Economic location still changes only through
engine travel processes; a server restart simply falls back to the scene's
deterministic place anchors.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

SPEED_TILES_PER_SECOND = 2.2
MAX_COMMANDS_PER_SECOND = 20


class PresenceError(ValueError):
    """A refused world-session command, with a client-safe reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Obstacle:
    x: float
    y: float
    radius: float


@dataclass
class Presence:
    connection_id: str
    entity_id: str
    owner_id: str
    place: str
    x: float
    y: float
    bounds: tuple[float, float, float, float]
    obstacles: tuple[Obstacle, ...] = ()
    dx: float = 0.0
    dy: float = 0.0
    last_update: float = field(default_factory=time.monotonic)
    last_seq: int = 0
    command_times: deque[float] = field(default_factory=deque)

    def advance(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        elapsed = max(0.0, now - self.last_update)
        if elapsed:
            self.x += self.dx * SPEED_TILES_PER_SECOND * elapsed
            self.y += self.dy * SPEED_TILES_PER_SECOND * elapsed
            self.last_update = now
        min_x, min_y, max_x, max_y = self.bounds
        self.x = max(min_x, min(max_x, self.x))
        self.y = max(min_y, min(max_y, self.y))
        self._push_out_of_obstacles()
        self._clamp()

    def _clamp(self) -> None:
        min_x, min_y, max_x, max_y = self.bounds
        self.x = max(min_x, min(max_x, self.x))
        self.y = max(min_y, min(max_y, self.y))

    def _inside_bounds(self, x: float, y: float) -> bool:
        min_x, min_y, max_x, max_y = self.bounds
        return min_x <= x <= max_x and min_y <= y <= max_y

    def _push_out_of_obstacles(self) -> None:
        actor_radius = 0.32
        for obstacle in self.obstacles:
            ox = self.x - obstacle.x
            oy = self.y - obstacle.y
            distance = math.hypot(ox, oy)
            minimum = actor_radius + obstacle.radius
            if distance >= minimum:
                continue
            if distance < 0.0001:
                ox, oy, distance = 1.0, 0.0, 1.0
            push = (minimum - distance) / distance
            nx = self.x + ox * push
            ny = self.y + oy * push
            if not self._inside_bounds(nx, ny):
                # At a narrow place edge the natural push can leave the walk
                # box; resolve through the obstacle's other side instead.
                nx = obstacle.x - (ox / distance) * minimum
                ny = obstacle.y - (oy / distance) * minimum
            if self._inside_bounds(nx, ny):
                self.x, self.y = nx, ny


class PresenceRuntime:
    """Thread-safe process-local presence store plus asyncio fan-out."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_entity: dict[str, Presence] = {}
        self._by_connection: dict[str, str] = {}
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._revision = 0

    def reset(self) -> None:
        """Test hook: clear all seats, positions, and subscribers."""
        with self._lock:
            self._by_entity.clear()
            self._by_connection.clear()
            self._revision = 0
        # Subscriber loops belong to closed test clients; dropping them is
        # intentional for an in-memory runtime reset.
        with self._lock:
            self._subscribers.clear()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [
                entry for entry in self._subscribers if entry[1] is not queue
            ]

    def _publish(self, payload: dict[str, Any]) -> None:
        with self._lock:
            entries = list(self._subscribers)
        for loop, queue in entries:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except RuntimeError:
                with self._lock:
                    self._subscribers = [
                        entry for entry in self._subscribers if entry[1] is not queue
                    ]

    def _actor_payload(self, presence: Presence, now: float | None = None) -> dict:
        presence.advance(now)
        return {
            "id": presence.entity_id,
            "place": presence.place,
            "position": {"x": round(presence.x, 3), "y": round(presence.y, 3)},
            "velocity": {"x": round(presence.dx, 3), "y": round(presence.dy, 3)},
            "bounds": {
                "x": presence.bounds[0], "y": presence.bounds[1],
                "width": presence.bounds[2] - presence.bounds[0],
                "height": presence.bounds[3] - presence.bounds[1],
            },
            "action": "walk",
            "control": "player",
        }

    def _emit(
        self, actors: list[dict] | None = None,
        released: list[str] | None = None, reason: str | None = None,
    ) -> None:
        with self._lock:
            self._revision += 1
            revision = self._revision
        payload: dict[str, Any] = {
            "type": "presence_delta",
            "revision": revision,
            "actors": actors or [],
            "released": released or [],
        }
        if reason is not None:
            payload["reason"] = reason
        self._publish(payload)

    def is_controlled(self, entity_id: str) -> bool:
        with self._lock:
            return entity_id in self._by_entity

    def attach(
        self, *, connection_id: str, entity_id: str, owner_id: str,
        place: str, position: dict[str, float],
        bounds: tuple[float, float, float, float],
        obstacles: list[dict[str, float]] | tuple[dict[str, float], ...] = (),
    ) -> dict:
        with self._lock:
            if entity_id in self._by_entity:
                raise PresenceError("entity is already controlled")
            if connection_id in self._by_connection:
                raise PresenceError("connection already controls an entity")
            presence = Presence(
                connection_id=connection_id,
                entity_id=entity_id,
                owner_id=owner_id,
                place=place,
                x=float(position["x"]),
                y=float(position["y"]),
                bounds=bounds,
                obstacles=tuple(
                    Obstacle(float(row["x"]), float(row["y"]), float(row["radius"]))
                    for row in obstacles
                ),
            )
            presence.advance()
            self._by_entity[entity_id] = presence
            self._by_connection[connection_id] = entity_id
            actor = self._actor_payload(presence)
        self._emit(actors=[actor])
        return actor

    def _touch(self, presence: Presence, seq: int, now: float) -> None:
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise PresenceError("seq must be a positive integer")
        if seq <= presence.last_seq:
            raise PresenceError("stale command sequence")
        while presence.command_times and now - presence.command_times[0] >= 1.0:
            presence.command_times.popleft()
        if len(presence.command_times) >= MAX_COMMANDS_PER_SECOND:
            raise PresenceError("command rate limit exceeded")
        presence.command_times.append(now)
        presence.last_seq = seq

    def _controlled(self, connection_id: str, entity_id: str, seq: int) -> Presence:
        with self._lock:
            presence = self._by_entity.get(entity_id)
            if presence is None or presence.connection_id != connection_id:
                raise PresenceError("entity is not controlled by this session")
            now = time.monotonic()
            presence.advance(now)
            self._touch(presence, seq, now)
            return presence

    def move(
        self, *, connection_id: str, entity_id: str, seq: int,
        dx: float, dy: float,
    ) -> dict:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in (dx, dy)):
            raise PresenceError("move direction must be numeric")
        dx, dy = float(dx), float(dy)
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise PresenceError("move direction must be finite")
        if math.hypot(dx, dy) > 1.0001:
            raise PresenceError("move direction must be normalized")
        with self._lock:
            presence = self._controlled(connection_id, entity_id, seq)
            presence.dx, presence.dy = dx, dy
            actor = self._actor_payload(presence)
        self._emit(actors=[actor])
        return actor

    def authorize(
        self, *, connection_id: str, entity_id: str, seq: int,
    ) -> Presence:
        """Validate a non-movement player command against the live seat."""
        return self._controlled(connection_id, entity_id, seq)

    def release(
        self, *, connection_id: str, entity_id: str, seq: int,
        reason: str = "released",
    ) -> str:
        with self._lock:
            self._controlled(connection_id, entity_id, seq)
            released = self._release_locked(entity_id)
        if released is not None:
            self._emit(released=[released], reason=reason)
            return released
        raise PresenceError("entity is not controlled by this session")

    def _release_locked(self, entity_id: str) -> str | None:
        presence = self._by_entity.pop(entity_id, None)
        if presence is None:
            return None
        self._by_connection.pop(presence.connection_id, None)
        return entity_id

    def detach_connection(self, connection_id: str, reason: str = "disconnected") -> str | None:
        with self._lock:
            entity_id = self._by_connection.get(connection_id)
            released = self._release_locked(entity_id) if entity_id else None
        if released is not None:
            self._emit(released=[released], reason=reason)
        return released

    def reconcile_scene(self, scene: dict) -> list[str]:
        """Engine processes take the steering wheel back from a human seat.

        Possession never pauses BEHAVIOUR. If that script (or any governed
        pass) has started work/travel/incapacity, local walking authority is
        released and the public scene projection becomes authoritative again.
        """
        conflicts = {
            actor["id"] for actor in scene.get("actors", [])
            if actor.get("action") in {"work", "travel", "incapacitated"}
        }
        with self._lock:
            released = [
                entity_id for entity_id in conflicts
                if self._release_locked(entity_id) is not None
            ]
        if released:
            self._emit(released=released, reason="engine_process")
        return released

    def snapshot(self) -> dict[str, dict]:
        """Current public overlays, keyed by entity id, for scene assembly."""
        now = time.monotonic()
        with self._lock:
            rows = list(self._by_entity.values())
            for presence in rows:
                presence.advance(now)
            self._separate_presences(rows)
            return {
                presence.entity_id: self._actor_payload(presence, now)
                for presence in rows
            }

    @staticmethod
    def _separate_presences(rows: list[Presence]) -> None:
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                if left.place != right.place:
                    continue
                ox = right.x - left.x
                oy = right.y - left.y
                distance = math.hypot(ox, oy)
                if distance >= 0.64:
                    continue
                if distance < 0.0001:
                    ox, oy, distance = 1.0, 0.0, 1.0
                push = (0.64 - distance) / distance / 2
                left.x -= ox * push
                left.y -= oy * push
                right.x += ox * push
                right.y += oy * push
        for presence in rows:
            presence._clamp()


runtime = PresenceRuntime()
