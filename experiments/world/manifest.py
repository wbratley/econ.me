"""The content-pack manifest (docs/scripting.md section 5, settled decision
#1: determinism pinning).

A pack is the unit of content management -- bundles of goods/tech/recipes/
cast/scripts -- and its manifest declares what the pack was AUTHORED
AGAINST: the engine-stdlib fingerprint and a sha per lua/ file. A world
running the pack refuses a mismatch (scenario.create_content calls
verify_manifest() before installing anything), so silent drift -- an engine
upgrade changing `std` semantics under a running world, an edited helper
nobody re-pinned -- is refused loudly instead of changing replay inputs.

The manifest is data (pack.json, shipped with the content, readable by
future platform tooling), regenerated deliberately:

    .venv/bin/python -m experiments.world.manifest

That rewrite is the pack author SAYING "I moved to the new baseline" --
which is exactly the moment replay semantics change.
"""

import hashlib
import json
import sys
from pathlib import Path

from econengine.lua_engine import stdlib_fingerprint

_ROOT = Path(__file__).parent
_LUA_DIR = _ROOT / "lua"
_MANIFEST_PATH = _ROOT / "pack.json"

PACK_NAME = "demo-world"

# --- The envelope (v1, docs/game.md 15.4: "the readable world" step 3d) ---
# A pack is more than its pins. The envelope is the pack's identity
# card, written next to the pins and shipped with the content, readable
# by future platform tooling without importing anything:
#   name / pack_id  -- who this is (v1: the name is the id, a slug)
#   version         -- the author's baseline marker, bumped deliberately
#   display         -- how a platform may title the pack to humans
#   requires        -- pack_ids this pack needs; empty means platform-only
#   content         -- what each scenario installs, counted from a real
#                      build at regen time (authored counts would drift;
#                      counted ones are as truthful as the pins)
PACK_VERSION = "0.3.0"
PACK_DISPLAY = {
    "title": "The Demo World",
    "summary": (
        "Two scenarios -- the frontier economy and the stone age -- "
        "plus the shared clerk, miner, smith and trading-post casts."
    ),
}
PACK_REQUIRES: list[str] = []   # v1: the platform is the only dependency


def pack_id() -> str:
    """The pack's stable id (v1: the name is the slug)."""
    return PACK_NAME


class PackManifestMismatch(ValueError):
    """The pack's files (or the engine stdlib) no longer match pack.json.

    Raised at install time; a world running this pack refuses the mismatch.
    Regenerate the manifest only when the new baseline is intended:
    `python -m experiments.world.manifest`."""


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def compute_pins() -> dict:
    """The manifest without the content counts: identity + pins.

    Safe to compute anywhere (including inside verify_manifest during a
    world install) -- it reads files and the engine stdlib, installs
    nothing."""
    lua = {path.name: _sha16(path.read_text())
           for path in sorted(_LUA_DIR.glob("*.lua"))}
    return {
        "name": PACK_NAME,
        "pack_id": pack_id(),
        "version": PACK_VERSION,
        "display": dict(PACK_DISPLAY),
        "requires": list(PACK_REQUIRES),
        "engine_std": stdlib_fingerprint(),
        "lua": lua,
    }


def compute_manifest() -> dict:
    """The full manifest as it would be for the CURRENT files + engine.

    Regen-time only: the content counts install throwaway worlds
    (see _content_counts), so nothing on the install path may call
    this -- verify_manifest compares against compute_pins instead."""
    manifest = compute_pins()
    manifest["content"] = _content_counts()
    return manifest


def _content_counts() -> dict[str, dict[str, int]]:
    """Rows each scenario installs, counted from a throwaway build.

    Computed at REGEN time only, against create_content(verify=False):
    counting is measurement, not installation, and at regen time the
    shipped pins are by definition stale -- the regen itself is the
    deliberate act the pins wait for. A test pins the shipped counts
    against a fresh build so edited content cannot slip past unnoticed.
    """
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from econengine.models import (
        Base, Good, Market, Need, Place, Recipe, Technology, Threat,
    )

    from . import scenario as frontier
    from . import stone_age

    counts: dict[str, dict[str, int]] = {}
    for name, builder in (("frontier", frontier), ("stone_age", stone_age)):
            engine = create_engine(
                "sqlite:///:memory:", connect_args={"check_same_thread": False},
                poolclass=StaticPool)
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                builder.create_content(session, verify=False)
                tables = {"goods": Good, "recipes": Recipe,
                          "technologies": Technology, "needs": Need,
                          "markets": Market, "threats": Threat,
                          "places": Place}
                counts[name] = {
                    label: session.scalar(
                        select(func.count()).select_from(m))
                    for label, m in tables.items()
                }
    return counts


def stamp_pack(session, owner: str | None = None) -> None:
    """Attribute every installed content row to this pack (15.4).

    NULL pack_id means platform/legacy content. The stamp runs once at
    install time, after create_content, so the catalog can say which
    pack shipped each row -- and a later install attempt on a claimed
    key is refused with the owner's name (the create_* conflict rule).
    """
    from sqlalchemy import update

    from econengine.models import Good, Market, Need, Place, Recipe, Technology

    owner = owner or pack_id()
    for model in (Good, Recipe, Technology, Need, Market, Place):
        session.execute(
            update(model).where(model.pack_id.is_(None))
            .values(pack_id=owner))
    session.flush()


def read_manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text())


def verify_manifest(manifest: dict | None = None) -> None:
    """Raise PackManifestMismatch on any drift between the shipped pack
    (files on disk + engine stdlib) and the manifest.

    The pins (engine_std, lua) are checked against the live engine and
    files; the envelope's identity fields must match exactly. The
    content counts are pinned by regen + tests, not recounted here --
    recounting would mean installing worlds inside every install."""
    manifest = manifest if manifest is not None else read_manifest()
    current = compute_pins()
    problems: list[str] = []
    for key in ("name", "pack_id", "version", "display", "requires"):
        if manifest.get(key) != current[key]:
            problems.append(
                f"{key}: manifest ships {manifest.get(key)!r}, "
                f"pack is {current[key]!r}"
            )
    shipped = manifest.get("content")
    if (not isinstance(shipped, dict)
            or not set(shipped) >= {"frontier", "stone_age"}):
        problems.append(
            f"content: manifest ships {sorted(shipped or [])}, "
            "pack installs ['frontier', 'stone_age']"
        )
    if manifest.get("engine_std") != current["engine_std"]:
        problems.append(
            f"engine stdlib: manifest pins {manifest.get('engine_std')!r}, "
            f"engine ships {current['engine_std']!r} -- the pack was "
            f"authored against different script vocabulary"
        )
    pinned = manifest.get("lua", {})
    for name, sha in current["lua"].items():
        if name not in pinned:
            problems.append(f"{name}: not pinned in the manifest")
        elif pinned[name] != sha:
            problems.append(
                f"{name}: manifest pins {pinned[name]}, file is {sha}"
            )
    for name in pinned:
        if name not in current["lua"]:
            problems.append(f"{name}: pinned but absent from lua/")
    if problems:
        raise PackManifestMismatch(
            "content pack drift: " + "; ".join(problems)
            + " -- regenerate deliberately: python -m experiments.world.manifest"
        )


def write_manifest() -> dict:
    manifest = compute_manifest()
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    written = write_manifest()
    print(f"wrote {_MANIFEST_PATH} ({len(written['lua'])} lua files, "
          f"engine_std {written['engine_std']})")
    sys.exit(0)
