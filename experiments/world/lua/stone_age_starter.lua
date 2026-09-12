-- stone_age_starter.lua  (BEHAVIOUR) -- the default a player inherits.
--
-- A hand-to-mouth loop with a commute (S4: the world has places, and
-- they are hours apart): keep the COMMONS fire fed and take a seat by
-- dark (warming is a seat, not a stock), EAT when the stomach runs low
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

local warmth = std.holding_qty("WARMTH")
local wood   = std.holding_qty("WOOD")
local meat   = std.holding_qty("MEAT")
local berries = std.holding_qty("BERRIES")
local apples = std.holding_qty("APPLES")
local cooked = std.holding_qty("COOKED_MEAT")
local jerky  = std.holding_qty("JERKY")
local food   = berries + apples + cooked + jerky

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

-- 1. The fire is common ground: a standing fire at the clearing seats
--    four and burns one fuel an hour, lit or empty. Keep it fed -- a
--    split log (STOKE_FIRE: 1 WOOD) buys two hours for everyone it
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

-- 1a. Sit when cold or when dark falls: the commons fire warms whoever
--     takes a seat while it burns (a dark fire warms no one -- feed it
--     first). Night draws 3 warmth an hour; a body holds six.
if (std.is_night() or warmth < 4) and #fires > 0
   and not std.running_recipe("WARM_BY_FIRE")
   and (place == home or can_walk) then
  if place ~= home then
    ctx.action.travel(home)
  else
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
if not std.is_night() then
  if (food < 8 or wood < 6) and not std.running_recipe("GATHER") then
    if place ~= woods then
      ctx.action.travel(woods)
    else
      ctx.action.start_process("GATHER")
    end
  elseif place ~= home and food >= 8 and wood >= 6 then
    ctx.action.travel(home)
  end
else
  -- caught out after dark: HOLD (the dark road wants a torch, and
  -- the floor carries none). The walk home is the first act of day.
end
