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


class PackManifestMismatch(ValueError):
    """The pack's files (or the engine stdlib) no longer match pack.json.

    Raised at install time; a world running this pack refuses the mismatch.
    Regenerate the manifest only when the new baseline is intended:
    `python -m experiments.world.manifest`."""


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def compute_manifest() -> dict:
    """The manifest as it would be for the CURRENT files + engine."""
    lua = {path.name: _sha16(path.read_text())
           for path in sorted(_LUA_DIR.glob("*.lua"))}
    return {
        "name": PACK_NAME,
        "engine_std": stdlib_fingerprint(),
        "lua": lua,
    }


def read_manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text())


def verify_manifest(manifest: dict | None = None) -> None:
    """Raise PackManifestMismatch on any drift between the shipped pack
    (files on disk + engine stdlib) and the manifest."""
    manifest = manifest if manifest is not None else read_manifest()
    current = compute_manifest()
    problems: list[str] = []
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
