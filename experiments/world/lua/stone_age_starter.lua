-- stone_age_starter.lua  (BEHAVIOUR) -- the default a player inherits.
--
-- A hand-to-mouth loop with a commute (S4: the world has places, and
-- they are hours apart): keep the COMMONS fire fed and sleep by it
-- through the dark (P4: warming is a seat, resting is a watch -- the
-- fire pays both while it burns), EAT when the stomach runs low
-- (eating is your decision now -- the engine no longer chews for you),
-- gather whatever the thicket offers, and WALK between them -- an hour
-- each way, the road exposed place by place. It survives -- barely. It
-- never builds capital (no spear, no bag, no shelter, no clothes, not
-- even its own fire -- the commons stands) and spends nearly every tick
-- on the next meal or the next log: that is the point. The stone-age
-- seat is POOR, and this script shows the floor. A player who changes
-- nothing stays on the treadmill; every escape -- tools, clothes,
-- shelter, trade -- is their invention.
--
-- The blocks are independent priorities, NOT one elseif chain: the
-- fire-block's first branch ("no fire yet") must not swallow the gather
-- fallback, or the seat deadlocks (no fire -> no wood -> no fire...).
-- At most one LABOR-paying action lands per tick (LABOR is the ration;
-- walking is labor-free but takes hours, so a travel intent is the
-- tick's act -- a second journey queued the same tick just bounces).
--
-- Vocabulary: std.* (engine), world.* (this world), pack.* (opinions).
-- Idioms worth copying (docs/scripting.md has the full surfaces):
--   where you stand (S4): ctx.entity.place is the key or nil
--     if ctx.entity.place ~= "THICKET" then ctx.action.travel("THICKET") end
--   price a walk before taking it:
--     local it = world.route(ctx.entity.place, "POST")
--   book prices are nil on an empty side -- ALWAYS pass a fallback:
--     local ask = std.best_ask("JERKY", 0)
--   a holding's spendable side is smaller than holding_qty when your
--   running processes reserve some of it:
--     if std.unreserved("LABOR") >= 1 then ... end

local place  = ctx.entity.place   -- where I stand (a place key, or nil)
local home   = "HEARTH"           -- the fire-ground: safe nights, the fire
local woods  = "THICKET"          -- the subsistence walk: food and wood
local river  = "RIVER"            -- the world's tap: free water at the bank

local warmth = std.holding_qty("WARMTH")
local rest   = std.holding_qty("REST")
local wood   = std.holding_qty("WOOD")
local meat   = std.holding_qty("MEAT")
local berries = std.holding_qty("BERRIES")
local apples = std.holding_qty("APPLES")
local cooked = std.holding_qty("COOKED_MEAT")
local jerky  = std.holding_qty("JERKY")
local food   = berries + apples + cooked + jerky
local water  = std.holding_qty("WATER") + std.holding_qty("SKINWATER")
local skin   = std.holding_qty("WATERSKIN")

-- 0. Eat: the stomach empties 0.5/hour plus a tenth of what's left.
--    Meals are labor-free, instant, night-legal and place-free -- but
--    they do not happen by themselves. Eat what spoils first (berries,
--    then apples, then cooked); jerky never rots, so it is the deep
--    pantry; raw meat is the desperate last resort (a one-in-four
--    chance of disease).
local satiety = std.holding_qty("SATIETY")
if satiety < 1.5 then
  if berries >= 2 then
    ctx.action.start_process("EAT_BERRIES")
  elseif apples >= 2 then
    ctx.action.start_process("EAT_APPLES")
  elseif cooked >= 1 then
    ctx.action.start_process("EAT_COOKED")
  elseif jerky >= 1 then
    ctx.action.start_process("EAT_JERKY")
  elseif meat >= 1 then
    ctx.action.start_process("EAT_RAW")
  end
end

-- 0a. Drink (P3): the second stomach draws 0.25/hour and meals only
--     part-hydrate -- the cup runs dry, and the river is the tap.
--     DRINK is free, instant and night-legal AT the bank; the walk is
--     a daylight matter (the floor carries no torch). While standing
--     there, top the skin first (it seeps, so "enough" is the bar:
--     the draw keeps any vessel just under full -- a body that waits
--     for a full cup stands in the river all day). Thirst kills on
--     the third dry day -- the floor treats an empty cup as urgent as
--     an empty stomach.
if place == river and skin >= 1 and std.holding_qty("SKINWATER") < 5 then
  ctx.action.start_process("FILL_SKIN")
elseif place == river and water < 2 then
  ctx.action.start_process("DRINK")
elseif water < 1 and not std.is_night() then
  ctx.action.travel(river)
end

-- 1. The fire is common ground: a standing fire at the clearing seats
--    four and burns one fuel an hour, lit or empty. Keep it fed -- a
--    split log (STOKE_FIRE: 1 WOOD) buys four hours for everyone it
--    warms, free and night-legal -- and take a seat by dark (warming
--    is a SEAT: +6 an hour, a body holds six). Both hearth-bound: if
--    the fire will want work, walk home first -- the walk is the
--    tick's act.
local fires = world.lit_fires(home)
local low_fuel = #fires == 0
for _, f in ipairs(fires) do
  if tonumber(f.fuel) <= 2 then low_fuel = true end
end
-- P2: the dark road wants a burning torch, and the starter carries
-- none -- so every walk is a DAYLIGHT act (a refused walk is a wasted
-- tick; by dark you are either home or you hold where you stand).
local can_walk = not std.is_night()
if low_fuel and wood >= 1 and (place == home or can_walk) then
  if place ~= home then
    ctx.action.travel(home)
  else
    ctx.action.start_process("STOKE_FIRE")
  end
end

-- 1a. Sleep or sit by dark (P4): the night draws 3 warmth an hour
--     AND a quarter-rest -- and an idle hour restores nothing. When
--     the body wants rest, SLEEP_BY_FIRE pays both draws at once
--     (REST +1, WARMTH +3 -- the fire does double duty) while it
--     burns. The WATCH is emergent, not scripted: sleep an hour, and
--     when the fuel runs low the stoke branch above takes the tick
--     between sleeps (fire fuel is the watch clock -- nobody sleeps,
--     keeps the fire, and stands watch all at once). The sleep wants
--     warmth BANKED first (>= 3: the stoke hour between sleeps draws
--     uncovered -- a seat before the watch, a seam never bit). Rested
--     and dark: take a seat as before (WARM_BY_FIRE, +6 -- the seat that
--     refills the stock a sleeping body only holds). A wolf that
--     finds a sleeper finds prey: sleeping holds no torch and little
--     warmth -- the price of the watch is vigilance's absence.
if (std.is_night() or warmth < 4) and #fires > 0
   and (place == home or can_walk) then
  if place ~= home then
    ctx.action.travel(home)
  elseif std.is_night() and rest < 5 and warmth >= 3
        and not std.running_recipe("SLEEP_BY_FIRE") then
    ctx.action.start_process("SLEEP_BY_FIRE")
  elseif not std.running_recipe("WARM_BY_FIRE") then
    ctx.action.start_process("WARM_BY_FIRE")
  end
end

-- 1b. Wolves are creatures that RANGE: by day the packs work the
--     forest game, and in the last daylight a hungry one walks the
--     road to the fire-ground -- the people's clearing is where it
--     prowls till dawn, so the dark's one law is simple: be home,
--     firelit. A lit hearth -- or a burning torch -- turns a pack at
--     the door. What bit you gets answered -- unarmed if it must
--     (fists are one hit in two; a spear in the rack is worth three).
--     The floor never says at night: speech carries to things that
--     listen.
local hits = std.holding_qty("HITS")
if hits < 20 and std.is_night() then
  for _, e in ipairs(ctx.events or {}) do
    if e.type == "combat" and e.target_id == ctx.entity.id
       and e.hit and e.entity_id ~= ctx.entity.id then
      ctx.action.attack(e.entity_id)
    end
  end
end

-- 2. Cooking: fire + 2 raw meat -> 2 safe food. The commons fire
--    cooks for anyone at the clearing (lit -- the starter stokes
--    first; coals do not cook).
if place == home and meat >= 2 and food < 4 then
  ctx.action.start_process("COOK_MEAT")
end

-- 3. Everything else is gathering at the thicket: food first, then wood
--    for the fire (wolves or no wolves, a dark camp is the expensive
--    one). Daylight only -- the dark refuses the work. Bigger loads
--    than the old floor: the commute is an hour each way, so the trip
--    pays for itself before the walk home. Stocked, or caught by dark
--    away from home: head back -- warmth is fatal to be without, food
--    buffers can wait out a night.
--    P4, dusk discipline: the night has a PLAN now -- sleep by a lit
--    fire -- and the plan starts before dark. Leave the woods when
--    the walk home would land in the dark (run 37's census: the twins
--    froze AT the thicket one road from their lit hearth; the floor
--    no longer models that death). Home before dark, stoked, asleep:
--    the watch is the reward for the commute's timing.
local walk_home = world.distance_ticks(woods, home) or 1
local dusk_closing = std.hour() + walk_home >= 19
if not std.is_night() then
  if (food < 8 or wood < 6) and not dusk_closing
     and not std.running_recipe("GATHER") then
    if place ~= woods then
      ctx.action.travel(woods)
    else
      ctx.action.start_process("GATHER")
    end
  elseif place ~= home and (dusk_closing or (food >= 8 and wood >= 6)) then
    ctx.action.travel(home)
  end
else
  -- caught out after dark: HOLD (the dark road wants a torch, and
  -- the floor carries none). The walk home is the first act of day.
end
