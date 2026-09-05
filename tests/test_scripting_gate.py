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


class TestCheckPlayerScript:
    """Phase 3: the submit-time lint for player-authored behaviours --
    (problems, warnings): problems refuse, warnings ride along."""

    _LIBS = {"world": "local w = {} function w.settle_last_orders() return {} end return w"}

    def test_nil_call_trap_is_a_problem(self):
        # The first live demo's zombie, verbatim: a helper from a prelude
        # that no longer exists. Refused at submit, not at the founder's
        # next tick.
        problems, warnings = scripting.check_player_script(
            "local fills = settle_last_orders()", libraries=self._LIBS)
        assert any("undeclared global 'settle_last_orders'" in p for p in problems)
        assert warnings == []

    def test_syntax_error_is_a_problem(self):
        problems, _ = scripting.check_player_script("local t = {")
        assert problems and problems[0].startswith("syntax:")

    def test_undeclared_write_is_a_problem(self):
        # One standard with the install gate: global scratch dies with the
        # per-run runtime; `local` is the fix.
        problems, _ = scripting.check_player_script("pending = {}", libraries=self._LIBS)
        assert any("assignment to undeclared global 'pending'" in p for p in problems)

    def test_tier_reassignment_is_a_problem(self):
        problems, _ = scripting.check_player_script("std = {}", libraries=self._LIBS)
        assert any("reassigned injected name" in p for p in problems)

    def test_state_dependent_error_is_only_a_warning(self):
        # A healthy script CAN error on the synthetic ctx (it has empty
        # state/events): nil arithmetic here, working behaviour at tick.
        # Accepted -- with the finding surfaced for the player to look at.
        problems, warnings = scripting.check_player_script(
            "ctx.state.hunger = ctx.state.hunger + 1", libraries=self._LIBS)
        assert problems == []
        assert len(warnings) == 1 and warnings[0].startswith("smoke-run:")

    def test_clean_tiered_script_passes_clean(self):
        problems, warnings = scripting.check_player_script(
            "local f = world.settle_last_orders() ctx.state.f = std.amount_str(1)",
            libraries=self._LIBS)
        assert (problems, warnings) == ([], [])

    def test_production_ctx_vocabulary_smoke_runs_clean(self):
        # Run 29 (Lagertha, d3h09): scripts reading ctx.clock crashed
        # ONLY at smoke time -- the synthetic ctx did not carry the key,
        # so healthy submissions landed as accept-with-warning (and the
        # agent harness's crash-retry held them hostage). The gate ctx
        # must tell the truth about vocabulary: clock, entity.place,
        # entity.age, entity.capabilities exist in production, so they
        # exist here. State-dependent crashes stay warnings; vocabulary
        # must never be.
        src = ("ctx.state.h = ctx.clock.hour "
               "ctx.state.p = ctx.entity.place "
               "ctx.state.a = ctx.entity.age "
               "ctx.state.c = ctx.entity.capabilities")
        problems, warnings = scripting.check_player_script(
            src, libraries=self._LIBS)
        assert (problems, warnings) == ([], [])


# --- Static shape lint (run 30 postmortem) --------------------------------
#
# House Ivar's fatal scripts, VERBATIM from the run-30 archive
# (~/econ-runs/stone-run30/world.db, entity 5b33e684). v3 is the round-2
# rewrite; v4 is the final active behaviour -- both read `.key` off the
# place string, a silent nil that behaved like "nowhere" until the house
# froze in the cold (d3h13, EXPOSURE). The lint must refuse these exact
# sources: regression value lives in the verbatim.

_IVAR_V3_SOURCE = """
local function holding(sym)
  for _, h in ipairs(ctx.holdings) do
    if h.symbol == sym then return tonumber(h.quantity) or 0 end
  end
  return 0
end

local function here_key()
  if ctx.entity.place then return ctx.entity.place.key end
  return nil
end

-- Always eat when the stomach is running low
if holding("SATIETY") < 2 then
  if holding("BERRIES") >= 1.5 then
    ctx.action.start_process("EAT_BERRIES")
  elseif holding("COOKED_MEAT") >= 1 then
    ctx.action.start_process("EAT_COOKED")
  elseif holding("JERKY") >= 1 then
    ctx.action.start_process("EAT_JERKY")
  elseif holding("MEAT") >= 1 then
    ctx.action.start_process("EAT_RAW")
  end
end

local here = here_key()
local home = "HEARTH"
local thicket = "THICKET"

if std.is_night() then
  -- Get to safety, then keep warm with PACE (free, labor-free)
  if here ~= home then
    ctx.action.travel(home)
  else
    if not std.running_recipe("PACE") then
      ctx.action.start_process("PACE")
    end
  end
else
  local hour = std.hour()
  -- Head home before dark
  if hour and hour >= 17 and here ~= home then
    ctx.action.travel(home)
  else
    local bag = holding("BAG")
    if bag < 1 then
      -- Try to make a bag once we have the wood; otherwise gather for it
      if holding("WOOD") >= 1 and holding("YARN") >= 2 then
        if not std.running_recipe("MAKE_BAG") then
          ctx.action.start_process("MAKE_BAG")
        end
      else
        if here == thicket then
          if not std.running_recipe("GATHER") then
            ctx.action.start_process("GATHER")
          end
        else
          ctx.action.travel(thicket)
        end
      end
    else
      -- Bag owned: gather with the better table
      if here == thicket then
        if not std.running_recipe("GATHER_BAG") then
          ctx.action.start_process("GATHER_BAG")
        end
      else
        ctx.action.travel(thicket)
      end
    end
  end
end
"""

_IVAR_V4_SOURCE = """
local function holding(sym)
  for _, h in ipairs(ctx.holdings) do
    if h.symbol == sym then return tonumber(h.quantity) or 0 end
  end
  return 0
end

local function balance()
  for _, a in ipairs(ctx.accounts) do
    if a.currency == "COIN" then return tonumber(a.balance) or 0 end
  end
  return 0
end

local function account_id()
  for _, a in ipairs(ctx.accounts) do
    if a.currency == "COIN" then return a.id end
  end
  return nil
end

local function running_rcp(recipe)
  for _, p in ipairs(ctx.processes) do
    if p.recipe == recipe then return true end
  end
  return false
end

-- Cancel any order we placed last tick (will be replaced if we still want it)
for _, e in ipairs(ctx.events) do
  if e.type == "place_order" and e.status == "applied" and e.order_id then
    ctx.action.cancel_order(e.order_id)
  end
end

local place = ctx.entity.place and ctx.entity.place.key or nil
local hour = std.hour() or 0
local night = std.is_night() or false
local acc = account_id()
local bal = balance()

-- Always eat if we have food and are hungry
if holding("SATIETY") < 3 then
  if holding("BERRIES") >= 1.5 then
    ctx.action.start_process("EAT_BERRIES")
  elseif holding("COOKED_MEAT") >= 1 then
    ctx.action.start_process("EAT_COOKED")
  elseif holding("JERKY") >= 1 then
    ctx.action.start_process("EAT_JERKY")
  elseif holding("MEAT") >= 1 then
    ctx.action.start_process("EAT_RAW")
  end
end

local safe_food = holding("BERRIES") + holding("COOKED_MEAT") + holding("JERKY")
local no_food_emergency = safe_food < 1.0 and holding("SATIETY") < 2

if night then
  if place == "HEARTH" then
    -- Only risk the walk after we have some warmth banked
    if no_food_emergency and holding("WARMTH") >= 3 then
      ctx.action.travel("POST")
    elseif not running_rcp("PACE") then
      ctx.action.start_process("PACE")
    end
  elseif place == "POST" then
    -- Buy a couple of jerky to survive until morning, but only once
    if not ctx.state.food_secured and acc and bal > 1.5 and holding("JERKY") < 1 then
      local ask = std.best_ask("JERKY") or 1.0
      local qty = 2
      if bal >= qty * ask then
        ctx.action.place_order("JERKY", "buy", std.amount_str(qty), std.amount_str(ask), acc)
      end
    end
    if holding("JERKY") >= 1 then
      ctx.state.food_secured = true
    end
    -- Always keep warm even at the trading post
    if not running_rcp("PACE") then
      ctx.action.start_process("PACE")
    end
  else
    -- Somewhere unexpected at night: head home immediately
    ctx.action.travel("HEARTH")
  end
else
  -- Day hours
  if hour and hour >= 17 and place ~= "HEARTH" then
    ctx.action.travel("HEARTH")
  else
    -- Build a bag once we have the mats
    if holding("BAG") < 1 and holding("WOOD") >= 1 and holding("YARN") >= 2 then
      if not running_rcp("MAKE_BAG") then
        ctx.action.start_process("MAKE_BAG")
      end
    else
      local gather_rcp = (holding("BAG") >= 1) and "GATHER_BAG" or "GATHER"
      if place == "THICKET" then
        if not running_rcp(gather_rcp) then
          ctx.action.start_process(gather_rcp)
        end
      else
        ctx.action.travel("THICKET")
      end
    end
  end
end
"""


class TestStaticShapeLint:
    """The two provable classes: member access on a known string path,
    and reads of members the injected namespaces do not carry."""

    _LIBS = {"world": "local w = {} function w.settle_last_orders() return {} end return w"}

    def test_ivar_v3_refused_verbatim(self):
        # The round-2 here_key() helper: `return ctx.entity.place.key`
        problems, _ = scripting.check_player_script(
            _IVAR_V3_SOURCE, libraries=self._LIBS)
        assert len(problems) == 1, problems  # precision: exactly the trap
        assert "ctx.entity.place.key" in problems[0]
        assert "KEY STRING" in problems[0]
        assert 'std.at("HEARTH")' in problems[0]

    def test_ivar_v4_refused_verbatim(self):
        # The final active behaviour: `ctx.entity.place and
        # ctx.entity.place.key or nil`. Everything else in the script is
        # sound -- one finding, the fix in hand.
        problems, _ = scripting.check_player_script(
            _IVAR_V4_SOURCE, libraries=self._LIBS)
        assert len(problems) == 1, problems
        assert "ctx.entity.place.key" in problems[0]
        assert "ctx.place.key" in problems[0]  # the facts-table alternative

    def test_correct_place_idioms_pass(self):
        src = (
            'local place = ctx.entity.place\n'
            'if place ~= "THICKET" and not std.at("HEARTH") then\n'
            '  local p = ctx.place\n'
            '  ctx.state.k = p and p.key or "?"\n'
            '  ctx.action.travel("THICKET")\n'
            'end\n'
        )
        problems, warnings = scripting.check_player_script(
            src, libraries=self._LIBS)
        assert (problems, warnings) == ([], [])

    def test_std_typo_refused_with_member_list(self):
        problems, _ = scripting.check_player_script(
            'return std.holding_qyt("GRAIN")', libraries=self._LIBS)
        assert any("std.holding_qyt does not exist" in p for p in problems)
        assert any("holding_qty" in p for p in problems)  # the real one listed

    def test_action_typo_refused_with_member_list(self):
        problems, _ = scripting.check_player_script(
            'ctx.action.travl("THICKET")', libraries=self._LIBS)
        assert any("ctx.action.travl does not exist" in p for p in problems)
        assert any("travel" in p for p in problems)

    def test_query_typo_refused(self):
        problems, _ = scripting.check_player_script(
            'return ctx.query.best_price("GRAIN")', libraries=self._LIBS)
        assert any("ctx.query.best_price does not exist" in p for p in problems)

    def test_commented_trap_is_not_a_finding(self):
        src = ('-- ctx.entity.place.key would be a trap, commented out\n'
               'return 1')
        problems, _ = scripting.check_player_script(src, libraries=self._LIBS)
        assert problems == []

    def test_trap_inside_string_literal_is_not_a_finding(self):
        src = 'ctx.state.note = "avoid ctx.entity.place.key" return 1'
        problems, _ = scripting.check_player_script(src, libraries=self._LIBS)
        assert problems == []

    def test_concat_after_place_is_not_a_finding(self):
        # `place .. "!"` is string concat, not member access
        src = 'ctx.state.s = ctx.entity.place .. "!" return 1'
        problems, _ = scripting.check_player_script(src, libraries=self._LIBS)
        assert problems == []


class TestMemberVocabulary:
    """The lint's member tables are pinned to the live truth: a drift
    here would refuse healthy scripts or bless broken ones."""

    def test_action_members_match_live_injection(self):
        known = {f'{m} = true' for m in scripting.ACTION_MEMBERS}
        src = (
            "local known = {" + ", ".join(sorted(known)) + "} "
            "local missing = {} "
            "for k in pairs(ctx.action) do "
            "  if not known[k] then missing[#missing+1] = k end "
            "end "
            "for _, m in ipairs({" +
            ", ".join(f'"{m}"' for m in scripting.ACTION_MEMBERS) + "}) do "
            "  if ctx.action[m] == nil then missing[#missing+1] = m end "
            "end "
            "if #missing > 0 then return missing[1] end "
            "return 'OK'"
        )
        result = scripting._engine.run(src, scripting.synthetic_ctx())
        assert result.error is None, result.error
        assert result.return_value == "OK"

    def test_query_members_match_build_queries(self, session):
        queries = scripting.build_queries(session)
        assert set(queries) == set(scripting.QUERY_MEMBERS)

    def test_std_members_carry_the_sugar(self):
        sugar = {"at", "need_level", "balance", "is_day"}
        core = {"holding_qty", "unreserved", "market_price", "best_bid",
                "best_ask", "has_unlock", "need_by_code", "running_recipe",
                "facility_parcel", "deposit_parcel", "amount_str", "hour",
                "day", "is_night"}
        assert sugar | core <= set(scripting.STDLIB_MEMBERS)


class TestSmokeMatrix:
    """Run 30 postmortem, part two: the gate ctx tells the full truth
    (placed entity, stocked pantry, live need, feed with a rejection)
    and the smoke-run becomes a matrix over day/night x hearth/thicket
    x stocked/empty -- so place-gated and pantry-gated code runs, not
    just its nil path. State-dependent findings stay warnings, now
    labeled with their state; self-bouncing travel intents -- run 30's
    silent killer (16 rejections unread) -- are warned, not refused."""

    _LIBS = {"world": "local w = {} function w.settle_last_orders() return {} end return w"}

    def test_smoke_states_cover_the_stone_age_grid(self):
        states = scripting.smoke_states()
        labels = [label for label, _ in states]
        assert len(states) == 8
        assert "day@hearth,stocked" in labels and "night@thicket,empty" in labels
        for label, overrides in states:
            light, at, pantry = label.split("@")[0], *label.split("@")[1].split(",")
            clock = overrides["clock"]
            assert clock["is_day"] == (light == "day")
            assert overrides["entity"]["place"] == at.upper()
            assert overrides["place"]["key"] == at.upper()
            assert (pantry == "stocked") == bool(overrides["holdings"])
            assert overrides["tick"] == clock["tick"]

    def test_synthetic_ctx_tells_the_truth_about_shapes(self):
        ctx = scripting.synthetic_ctx()
        assert ctx["entity"]["place"] == "HEARTH"          # key STRING
        assert ctx["place"]["key"] == "HEARTH"             # facts table
        assert ctx["places"][0]["key"] == "HEARTH"
        assert ctx["holdings"][0]["quantity"] == "3.0000"  # string decimals
        need = ctx["needs"][0]
        assert need["code"] == "WARMTH" and need["satisfaction"] == "0.5000"
        assert need["satisfiers"] == ["WARMTH"] and need["condition"] == "EXPOSURE"
        assert ctx["events"][-1]["status"] == "rejected"   # reason in hand
        assert "already at" in ctx["events"][-1]["reason"]

    def test_the_real_starter_passes_clean(self):
        from pathlib import Path
        starter = (Path(__file__).parents[1]
                   / "experiments" / "world" / "lua" / "stone_age_starter.lua")
        problems, warnings = scripting.check_player_script(starter.read_text())
        assert (problems, warnings) == ([], [])

    def test_reading_truthful_rows_runs_clean(self):
        src = (
            'local h = 0\n'
            'if #ctx.holdings > 0 then\n'
            '  h = tonumber(ctx.holdings[1].quantity) or 0\n'
            'end\n'
            'local n = ctx.needs[1]\n'
            'local s = tonumber(n.satisfaction) or 0\n'
            'if ctx.entity.place == "HEARTH" and s < 1 and h > 0 then\n'
            '  ctx.action.travel("THICKET")\n'
            'end\n'
        )
        problems, warnings = scripting.check_player_script(
            src, libraries=self._LIBS)
        assert (problems, warnings) == ([], [])

    def test_travel_to_wherever_i_stand_warns_per_target(self):
        # The unconditional-travel specimen: `travel(ctx.entity.place)`
        # is a bounce wherever the entity stands -- the run-30 killer
        # in its purest form. One warning per distinct target, each
        # with its state count.
        problems, warnings = scripting.check_player_script(
            "ctx.action.travel(ctx.entity.place)", libraries=self._LIBS)
        assert problems == []
        assert len(warnings) == 2
        assert all("bounces" in w and "4/8 gate states" in w for w in warnings)
        assert any("already at HEARTH" in w for w in warnings)
        assert any("already at THICKET" in w for w in warnings)

    def test_unconditional_home_travel_warns_in_hearth_states(self):
        # The fixed-Ivar shape: place read correctly, but the branch
        # still fires travel("HEARTH") while standing at the hearth.
        src = (
            'local place = ctx.entity.place\n'
            'if std.is_night() or place ~= "THICKET" then\n'
            '  ctx.action.travel("HEARTH")\n'
            'end\n'
        )
        problems, warnings = scripting.check_player_script(
            src, libraries=self._LIBS)
        assert problems == []
        bounce = [w for w in warnings if "bounces" in w]
        assert len(bounce) == 1 and "4/8 gate states" in bounce[0]

    def test_state_labeled_warning_for_partial_errors(self):
        # Errors in SOME states stay warnings, labeled per state -- a
        # script that only misbehaves on an empty pantry is healthy
        # half the time; the label says exactly which half.
        src = (
            'if #ctx.holdings == 0 then\n'
            '  ctx.state.q = ctx.holdings[1].quantity .. "!"\n'
            'end\n'
        )
        problems, warnings = scripting.check_player_script(
            src, libraries=self._LIBS)
        assert problems == []
        assert len(warnings) == 1
        assert warnings[0].startswith("smoke-run[")
        assert "empty" in warnings[0]
