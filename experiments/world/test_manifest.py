"""The content-pack manifest (docs/scripting.md section 5, settled decision
#1; Phase 2): pack.json pins the engine-stdlib fingerprint and a sha per
lua/ file. A world running this pack refuses drift at install time."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econengine.models import Base
from experiments.world import manifest


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_shipped_manifest_is_current():
    # The committed pack.json matches the shipped lua/ files AND this
    # engine's stdlib. If this fails after editing content or upgrading the
    # engine: that is the point -- regenerate deliberately with
    #   .venv/bin/python -m experiments.world.manifest
    manifest.verify_manifest()


def test_drift_is_refused():
    good = manifest.compute_manifest()
    assert good["name"] == manifest.PACK_NAME
    assert len(good["lua"]) >= 6  # all role scripts + world/pack libs + clerk

    tampered = {**good, "engine_std": "0" * 16}
    with pytest.raises(manifest.PackManifestMismatch, match="engine stdlib"):
        manifest.verify_manifest(tampered)

    tampered = {**good, "lua": {**good["lua"], "pack.lua": "0" * 16}}
    with pytest.raises(manifest.PackManifestMismatch, match="pack.lua"):
        manifest.verify_manifest(tampered)

    tampered = {**good, "lua": {k: v for k, v in good["lua"].items()
                                if k != "smith.lua"}}
    with pytest.raises(manifest.PackManifestMismatch, match="smith.lua"):
        manifest.verify_manifest(tampered)


def test_manifest_roundtrip_is_stable():
    current = manifest.compute_manifest()
    # write -> read -> verify is a fixed point
    path = manifest._MANIFEST_PATH
    original = path.read_text()
    try:
        manifest.write_manifest()
        assert manifest.read_manifest() == current
        manifest.verify_manifest()
    finally:
        path.write_text(original)


def test_bootstrap_refuses_a_tampered_manifest(tmp_path, session, monkeypatch):
    """A world running this pack refuses the mismatch: create_content
    (the pack install path) raises before anything is seeded."""
    from econengine import scripting
    from experiments.world import scenario

    good = manifest.compute_manifest()
    tampered = {**good, "engine_std": "0" * 16}
    monkeypatch.setattr(manifest, "read_manifest", lambda: tampered)

    with pytest.raises(manifest.PackManifestMismatch):
        scenario.create_content(session)
    # And nothing was installed -- the refusal came first.
    assert scripting.get_world_lib(session) is None
    assert scripting.get_pack_lib(session) is None
