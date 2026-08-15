-- The governance-window clerk (docs/game.md §14.4).
--
-- A POLICY script on the server-owned polity entity (the "Assembly").
-- Each tick it reads `round.state` -- the same WorldSetting any script
-- reads -- and derives the window calendar: round r is a window round
-- iff r % N == 0, where N rides in round.state as `rounds_per_window`
-- (projected from deployment config by each advance; the env stays the
-- single source of truth).
--
-- On a window close it sweeps the docket: every OPEN proposal is decided
-- by the ordinary `enact` intent -- the engine's tally either applies the
-- mutations as this polity (capability gates and VALIDATORs fire as for a
-- live intent) or marks the proposal FAILED. Out-of-window proposals are
-- legal but dormant; this sweep is the only moment they take effect.
--
-- `ctx.state.last_window_swept` makes the sweep once-per-window-close:
-- round r stays in round.state for all K ticks of round r+1, and every
-- one of those ticks would otherwise re-decide the docket.

local rs = ctx.query.world_setting("round.state")
if rs == nil then
  return  -- no rounds have resolved yet (raw-tick world or genesis)
end

local n = rs.rounds_per_window or 5
local r = rs.round_number or 0

if r % n ~= 0 then
  return  -- not a window close; the docket stays dormant
end

if (ctx.state.last_window_swept or -1) >= r then
  return  -- already swept this window (the K ticks of the next round all
          -- see the same round_number)
end

ctx.state.last_window_swept = r
for _, p in ipairs(ctx.query.proposals("open")) do
  ctx.action.enact(p.id)
end
