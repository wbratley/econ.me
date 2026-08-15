"""The install-time gate + version pinning (docs/scripting.md section 4 and
settled decision #1; Phase 2). Nothing broken reaches a tick: world/pack
libs are refused at set time, pack scripts at build time, and the engine
stdlib's identity is pinnable so drift under a running world is visible."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econengine import scripting
from econengine.lua_engine import stdlib_fingerprint
from econengine.models import Base


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


_GOOD_LIB = "local t = {} function t.f(x) return (x or 1) * 2 end return t"


class TestValidateLibrarySource:
    def test_good_library_passes(self):
        assert scripting.validate_library_source(_GOOD_LIB) == []

    def test_syntax_error(self):
        problems = scripting.validate_library_source("local t = {")
        assert len(problems) == 1 and problems[0].startswith("syntax:")

    def test_non_table_return(self):
        problems = scripting.validate_library_source("return 5")
        assert any("namespace table" in p for p in problems)

    def test_scalar_member_rejected(self):
        problems = scripting.validate_library_source("local t = {} t.N = 5 return t")
        assert any("member 'N'" in p for p in problems)

    def test_undeclared_global_write_in_body(self):
        problems = scripting.validate_library_source(
            "local t = {} fctor = {} return t")
        assert any("assignment to undeclared global" in p for p in problems)

    def test_nil_call_inside_member_body_caught_by_sweep(self):
        # The sweep calls each member under strict globals; the typo'd
        # helper read surfaces here, not at a player's tick.
        problems = scripting.validate_library_source(
            "local t = {} function t.f() return setle_last() end return t")
        assert any("setle_last" in p and "member sweep" in p for p in problems)

    def test_arity_noise_is_not_a_finding(self):
        # concede(fills, symbol) with nil args errors on concat -- that is
        # signature mismatch, not the lint's target class; it must NOT
        # reject the library.
        lib = ("local t = {} function t.f(a, b) return a .. b end return t")
        assert scripting.validate_library_source(lib) == []


class TestValidateScriptSource:
    def test_the_historical_zombie_trap_is_refused(self):
        # The exact failure from the first live demo: a starter stored
        # without its world's prelude nil-called settle_last_orders every
        # tick and zombied quietly. The gate says no, at submit time.
        problems = scripting.validate_script_source(
            "local fills = settle_last_orders()",
            libraries={"world": "local w = {} function w.settle_last_orders() return {} end return w"},
        )
        assert any("undeclared global 'settle_last_orders'" in p for p in problems)

    def test_tiered_script_passes(self):
        problems = scripting.validate_script_source(
            "local f = world.settle_last_orders() ctx.state.x = std.amount_str(1)",
            libraries={"world": "local w = {} function w.settle_last_orders() return {} end return w"},
        )
        assert problems == []


class TestGatedSetters:
    def test_set_world_lib_refuses_broken_source(self, session):
        with pytest.raises(scripting.LibraryRejected):
            scripting.set_world_lib(session, "return 5")
        assert scripting.get_world_lib(session) is None

    def test_set_pack_lib_roundtrip(self, session):
        assert scripting.set_pack_lib(session, _GOOD_LIB) == _GOOD_LIB
        assert scripting.get_pack_lib(session) == _GOOD_LIB
        scripting.set_pack_lib(session, None)
        assert scripting.get_pack_lib(session) is None

    def test_libraries_accessor_carries_both_tiers(self, session):
        assert scripting.get_world_libraries(session) is None
        scripting.set_world_lib(session, _GOOD_LIB)
        scripting.set_pack_lib(session, _GOOD_LIB)
        assert scripting.get_world_libraries(session) == {
            "world": _GOOD_LIB, "pack": _GOOD_LIB,
        }


class TestVersionPinning:
    def test_pin_and_report_match(self, session):
        scripting.pin_std_version(session)
        report = scripting.scripting_report(session)
        assert report["std"]["pinned"] == stdlib_fingerprint()
        assert report["std"]["matches_pinned"] is True

    def test_drift_is_visible(self, session):
        scripting.pin_std_version(session)
        row = session.get(
            __import__("econengine.models", fromlist=["WorldSetting"]).WorldSetting,
            scripting.STD_PIN_KEY)
        row.value = "0" * 16  # simulate an engine upgrade under a running world
        session.flush()
        assert scripting.scripting_report(session)["std"]["matches_pinned"] is False

    def test_report_gates_installed_libs(self, session):
        scripting.set_world_lib(session, _GOOD_LIB)
        report = scripting.scripting_report(session)
        assert report["gate"]["world_lib"] == []
        assert report["world_lib_sha"] is not None
