-- stone_age_starter.lua  (BEHAVIOUR) -- the default a player inherits.
--
-- A hand-to-mouth loop with a commute (S4: the world has places, and
-- they are hours apart): keep a fire at the hearth, feed it logs when
-- the warmth runs low, EAT when the stomach runs low (eating is your
-- decision now -- the engine no longer chews for you), gather whatever
-- the thicket offers, and WALK between them -- an hour each way, the
-- road exposed place by place. It survives -- barely. It never builds
-- capital (no spear, no bag, no shelter, no clothes) and spends nearly
-- every tick on the next meal or the next log: that is the point. The
-- stone-age seat is POOR, and this script shows the floor. A player
-- who changes nothing stays on the treadmill; every escape -- tools,
-- clothes, shelter, trade -- is their invention.
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

local camp = ctx.parcels[1] and ctx.parcels[1].id
local fire = std.facility_parcel("FIRE")

local place  = ctx.entity.place   -- where I stand (a place key, or nil)
local home   = "HEARTH"           -- the fire-ground: safe nights, the fire
local woods  = "THICKET"          -- the subsistence walk: food and wood

local warmth = std.holding_qty("WARMTH")
local wood   = std.holding_qty("WOOD")
local meat   = std.holding_qty("MEAT")
local berries = std.holding_qty("BERRIES")
local cooked = std.holding_qty("COOKED_MEAT")
local jerky  = std.holding_qty("JERKY")
local food   = berries + cooked + jerky

-- 0. Eat: the stomach empties 0.5/hour plus a tenth of what's left.
--    Meals are labor-free, instant, night-legal and place-free -- but
--    they do not happen by themselves. Eat what spoils first (berries,
--    then cooked); jerky never rots, so it is the deep pantry; raw
--    meat is the desperate last resort (a one-in-four chance of
--    disease).
local satiety = std.holding_qty("SATIETY")
if satiety < 1.5 then
  if berries >= 2 then
    ctx.action.start_process("EAT_BERRIES")
  elseif cooked >= 1 then
    ctx.action.start_process("EAT_COOKED")
  elseif jerky >= 1 then
    ctx.action.start_process("EAT_JERKY")
  elseif meat >= 1 then
    ctx.action.start_process("EAT_RAW")
  end
end

-- 1. The fire: at the hearth. Night draws 3 warmth an hour -- bank a
--    stock before dark (hour 15 onward) and keep the fire fed while
--    labor lasts into the evening. MAKE_FIRE and TEND_FIRE are
--    night-legal (darkness only refuses gathering and hunting) but
--    hearth-bound: if the warmth will want work, walk home first --
--    the walk is the tick's act.
if not fire then
  if wood >= 2 then
    if place ~= home then
      ctx.action.travel(home)
    else
      ctx.action.start_process("MAKE_FIRE", camp)
    end
  end
elseif warmth < (std.hour() and std.hour() >= 15 and 12 or 4)
       and wood >= 1 and not std.running_recipe("TEND_FIRE") then
  if place ~= home then
    ctx.action.travel(home)
  else
    ctx.action.start_process("TEND_FIRE", fire)
  end
end

-- 1b. Wolves are creatures that RANGE: by day the packs work the
--     forest game, by night a hungry one walks -- the fire-ground is
--     where the people sleep, so the dark's one law is simple: be
--     home, firelit. A lit hearth turns a pack at the door. What bit
--     you gets answered -- unarmed if it must (fists are one hit in
--     two; a spear in the rack is worth three). The floor never says
--     at night: speech carries to things that listen.
local hits = std.holding_qty("HITS")
if hits < 20 and std.is_night() then
  for _, e in ipairs(ctx.events or {}) do
    if e.type == "combat" and e.target_id == ctx.entity.id
       and e.hit and e.entity_id ~= ctx.entity.id then
      ctx.action.attack(e.entity_id)
    end
  end
end

-- 2. Cooking: fire + 2 raw meat -> 2 safe food. The fire is the camp's,
--    and the camp is at the hearth -- cook while home.
if fire and place == home and meat >= 2 and food < 4 then
  ctx.action.start_process("COOK_MEAT", fire)
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
  if place ~= home then
    ctx.action.travel(home)
  end
end
