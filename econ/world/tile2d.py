"""The first world-layer geometry: deterministic ``tile2d.v1`` chunks.

Terrain is a pure function of ``(seed, generator_version, tile x, tile y)``.
Engine places are anchored by world-layer configuration (or their opaque
``extent_ref``), with a deterministic topology-independent fallback layout.
Nothing here writes to the engine database or gives terrain economic meaning.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from econengine.models import Place, WorldSetting

GEOMETRY_ID = "tile2d.v1"
CONFIG_KEY = "world_layer.tile2d"
DEFAULT_GENERATOR = "meadow-v1"
EXTENT_PREFIX = "tile2d:v1:"

TILES = (
    {"id": 0, "key": "water", "color": "#4f8391", "role": "decor"},
    {"id": 1, "key": "sand", "color": "#c2ad73", "role": "decor"},
    {"id": 2, "key": "grass", "color": "#6f9657", "role": "decor"},
    {"id": 3, "key": "meadow", "color": "#7da460", "role": "decor"},
    {"id": 4, "key": "moss", "color": "#527b4f", "role": "decor"},
    {"id": 5, "key": "flowers", "color": "#789c5b", "role": "decor"},
    {"id": 6, "key": "stone", "color": "#85877a", "role": "decor"},
    {"id": 7, "key": "earth", "color": "#927956", "role": "decor"},
)


class WorldConfigError(ValueError):
    """The installed world-layer geometry is not safely renderable."""


@dataclass(frozen=True)
class Tile2DConfig:
    seed: str = "econ.me"
    generator_version: str = DEFAULT_GENERATOR
    chunk_size: int = 16
    tile_size: int = 16
    width_chunks: int = 8
    height_chunks: int = 6
    place_anchors: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def width_tiles(self) -> int:
        return self.width_chunks * self.chunk_size

    @property
    def height_tiles(self) -> int:
        return self.height_chunks * self.chunk_size

    @property
    def cache_key(self) -> str:
        value = {
            "seed": self.seed,
            "generator": self.generator_version,
            "chunk_size": self.chunk_size,
            "width_chunks": self.width_chunks,
            "height_chunks": self.height_chunks,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _bounded_int(raw: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise WorldConfigError(f"{name} must be an integer")
    if not minimum <= raw <= maximum:
        raise WorldConfigError(f"{name} must be between {minimum} and {maximum}")
    return raw


def _text(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 128:
        raise WorldConfigError(f"{name} must be a non-empty string of at most 128 characters")
    return raw.strip()


def load_config(session: Session) -> Tile2DConfig:
    """Read optional platform/pack geometry data; absent means a safe default.

    This is deliberately not an engine setting consulted by ticks. It lives
    in WorldSetting only to avoid a migration for the read-only W1 slice.
    """
    row = session.get(WorldSetting, CONFIG_KEY)
    raw: dict[str, Any] = {} if row is None else row.value
    if not isinstance(raw, dict):
        raise WorldConfigError(f"{CONFIG_KEY} must be an object")
    allowed = {
        "seed", "generator_version", "chunk_size", "tile_size",
        "width_chunks", "height_chunks", "place_anchors",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise WorldConfigError(f"unknown tile2d settings: {', '.join(unknown)}")

    anchors_raw = raw.get("place_anchors", {})
    if not isinstance(anchors_raw, dict):
        raise WorldConfigError("place_anchors must be an object keyed by place")
    anchors: dict[str, tuple[int, int]] = {}
    for key, point in anchors_raw.items():
        if not isinstance(key, str) or not key:
            raise WorldConfigError("place anchor keys must be non-empty strings")
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) for v in point)):
            raise WorldConfigError(f"place anchor {key!r} must be [x, y] integers")
        anchors[key.upper()] = (point[0], point[1])

    config = Tile2DConfig(
        seed=_text(raw.get("seed", "econ.me"), "seed"),
        generator_version=_text(
            raw.get("generator_version", DEFAULT_GENERATOR), "generator_version"),
        chunk_size=_bounded_int(raw.get("chunk_size", 16), "chunk_size", 4, 32),
        tile_size=_bounded_int(raw.get("tile_size", 16), "tile_size", 4, 64),
        width_chunks=_bounded_int(raw.get("width_chunks", 8), "width_chunks", 1, 64),
        height_chunks=_bounded_int(raw.get("height_chunks", 6), "height_chunks", 1, 64),
        place_anchors=anchors,
    )
    for key, (x, y) in anchors.items():
        if not (0 <= x < config.width_tiles and 0 <= y < config.height_tiles):
            raise WorldConfigError(f"place anchor {key!r} lies outside the world")
    return config


def extent_point(extent_ref: str | None) -> tuple[int, int] | None:
    """Resolve the W1 point form of an opaque engine extent reference.

    Other geometry references (and legacy values such as ``chunk:0,0``) are
    simply not this adapter's business and fall back to the generated layout.
    """
    if not extent_ref or not extent_ref.startswith(EXTENT_PREFIX):
        return None
    raw = extent_ref[len(EXTENT_PREFIX):]
    try:
        x_text, y_text = raw.split(",", 1)
        return int(x_text), int(y_text)
    except (TypeError, ValueError):
        raise WorldConfigError(
            f"invalid {GEOMETRY_ID} extent point; expected {EXTENT_PREFIX}<x>,<y>")


def _hash_int(config: Tile2DConfig, channel: str, *parts: object) -> int:
    value = "|".join([
        config.seed, config.generator_version, channel,
        *(str(part) for part in parts),
    ])
    digest = hashlib.blake2b(value.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _hash01(config: Tile2DConfig, channel: str, *parts: object) -> float:
    return _hash_int(config, channel, *parts) / float(2**64 - 1)


def _noise(config: Tile2DConfig, x: int, y: int, scale: int, channel: str) -> float:
    """Continuous value noise; chunk borders therefore cannot create seams."""
    gx, rx = divmod(x, scale)
    gy, ry = divmod(y, scale)
    tx = rx / scale
    ty = ry / scale
    tx = tx * tx * (3.0 - 2.0 * tx)
    ty = ty * ty * (3.0 - 2.0 * ty)

    def value(ix: int, iy: int) -> float:
        return _hash01(config, channel, ix, iy)

    top = value(gx, gy) * (1.0 - tx) + value(gx + 1, gy) * tx
    bottom = value(gx, gy + 1) * (1.0 - tx) + value(gx + 1, gy + 1) * tx
    return top * (1.0 - ty) + bottom * ty


def tile_at(config: Tile2DConfig, x: int, y: int) -> int:
    """One cosmetic terrain tile. It is never a resource or ownership fact."""
    height = (
        0.68 * _noise(config, x, y, 18, "height")
        + 0.32 * _noise(config, x, y, 7, "detail")
    )
    moisture = _noise(config, x + 4096, y - 4096, 13, "moisture")
    if height < 0.20:
        return 0  # water
    if height < 0.26:
        return 1  # sand
    if height > 0.84:
        return 6  # stone
    if height > 0.76:
        return 7  # earth
    decoration = _hash01(config, "decoration", x, y)
    if moisture > 0.69:
        return 4 if decoration > 0.42 else 3
    if decoration > 0.965:
        return 5  # flowers
    return 3 if decoration > 0.74 else 2


def generate_chunk(config: Tile2DConfig, cx: int, cy: int) -> dict:
    if not (0 <= cx < config.width_chunks and 0 <= cy < config.height_chunks):
        raise KeyError((cx, cy))
    size = config.chunk_size
    tiles = [
        tile_at(config, x, y)
        for y in range(cy * size, (cy + 1) * size)
        for x in range(cx * size, (cx + 1) * size)
    ]
    digest = hashlib.sha256(config.cache_key.encode() + bytes(tiles)).hexdigest()
    return {
        "geometry": GEOMETRY_ID,
        "generator_version": config.generator_version,
        "cache_key": config.cache_key,
        "revision": digest[:16],
        "cx": cx,
        "cy": cy,
        "width": size,
        "height": size,
        "tiles": tiles,  # row-major indexes into the bootstrap tile manifest
    }


def place_positions(
    places: list[Place], config: Tile2DConfig,
) -> dict[str, dict[str, float]]:
    """Return deterministic world-layer anchors, never persisted engine data.

    Explicit config wins, then ``Place.extent_ref``. The fallback is a stable
    jittered grid; it makes old packs visible without pretending their engine
    topology already contains coordinates.
    """
    ordered = sorted(places, key=lambda place: place.key)
    if not ordered:
        return {}
    width, height = config.width_tiles, config.height_tiles
    aspect = width / max(1, height)
    columns = max(1, math.ceil(math.sqrt(len(ordered) * aspect)))
    rows = max(1, math.ceil(len(ordered) / columns))
    cell_w, cell_h = width / columns, height / rows
    positions: dict[str, dict[str, float]] = {}

    for index, place in enumerate(ordered):
        col, row = index % columns, index // columns
        jitter_x = (_hash01(config, "place-x", place.key) - 0.5) * cell_w * 0.34
        jitter_y = (_hash01(config, "place-y", place.key) - 0.5) * cell_h * 0.34
        x = (col + 0.5) * cell_w + jitter_x
        y = (row + 0.5) * cell_h + jitter_y
        point = config.place_anchors.get(place.key)
        if point is None:
            point = extent_point(place.extent_ref)
        if point is not None:
            x, y = point
            if not (0 <= x < width and 0 <= y < height):
                raise WorldConfigError(f"place {place.key!r} extent lies outside the world")
        positions[place.key] = {
            "x": round(max(1.0, min(width - 1.0, float(x))), 3),
            "y": round(max(1.0, min(height - 1.0, float(y))), 3),
        }
    return positions


def ring_position(
    anchor: dict[str, float], config: Tile2DConfig, key: str,
    index: int, count: int, radius: float,
) -> dict[str, float]:
    """Deterministic presentation offsets for actors/objects sharing a place."""
    if count <= 1:
        return {"x": anchor["x"], "y": anchor["y"]}
    start = _hash01(config, "ring", key) * math.tau
    angle = start + math.tau * index / max(1, min(count, 8))
    distance = radius * (1.0 + 0.45 * (index // 8))
    return {
        "x": round(max(0.5, min(config.width_tiles - 0.5,
                                anchor["x"] + math.cos(angle) * distance)), 3),
        "y": round(max(0.5, min(config.height_tiles - 0.5,
                                anchor["y"] + math.sin(angle) * distance)), 3),
    }


def geometry_descriptor(config: Tile2DConfig) -> dict:
    return {
        "id": GEOMETRY_ID,
        "kind": "tile2d",
        "version": 1,
        "generator_version": config.generator_version,
        "cache_key": config.cache_key,
        "seed": config.seed,
        "tile_size": config.tile_size,
        "chunk_size": config.chunk_size,
        "world": {"x": 0, "y": 0, "width": config.width_tiles,
                  "height": config.height_tiles},
        "coordinates": "tile units; origin top-left; +x east, +y south",
    }


def asset_manifest() -> dict:
    """Semantic art bindings: clients need no recipe-specific renderer."""
    return {
        "tiles": [dict(tile) for tile in TILES],
        "actor": {
            "frame_width": 12,
            "frame_height": 16,
            "animations": {
                "idle": {"frames": [0, 1], "frame_ms": 700},
                "work": {"frames": [2, 3], "frame_ms": 280},
                "walk": {"frames": [4, 5], "frame_ms": 180},
                "travel": {"frames": [4, 5, 6, 7], "frame_ms": 160},
                "incapacitated": {"frames": [8], "frame_ms": 1000},
            },
        },
        "note": "Reference sprites are drawn from these semantic keys; atlases can replace them without protocol changes.",
    }
