-- stone_age_starter.lua  (BEHAVIOUR) -- the default a player inherits.
--
-- A hand-to-mouth loop: keep a fire, feed it logs when the warmth runs
-- low, EAT when the stomach runs low (eating is your decision now --
-- the engine no longer chews for you), cook meat when there is any,
-- gather whatever the forest offers.
-- It survives -- barely. It never builds capital (no spear, no bag, no
-- shelter, no clothes) and spends nearly every tick on the next meal or
-- the next log: that is the point. The stone-age seat is POOR, and this
-- script shows the floor. A player who changes nothing stays on the
-- treadmill; every escape -- tools, clothes, shelter, trade -- is their
-- invention.
--
-- The blocks are independent priorities, NOT one elseif chain: the
-- fire-block's first branch ("no fire yet") must not swallow the gather
-- fallback, or the seat deadlocks (no fire -> no wood -> no fire...).
-- At most one LABOR-paying action lands per tick (LABOR is the ration);
-- the blocks just set the priority order in which intents queue.
--
-- Vocabulary: std.* (engine), world.* (this world), pack.* (opinions).
-- Idioms worth copying (docs/scripting.md has the full surfaces):
--   book prices are nil on an empty side -- ALWAYS pass a fallback:
--     local ask = std.best_ask("JERKY", 0)
--   a holding's spendable side is smaller than holding_qty when your
--   running processes reserve some of it:
--     if std.unreserved("LABOR") >= 1 then ... end

local camp = ctx.parcels[1] and ctx.parcels[1].id
local fire = std.facility_parcel("FIRE")

local warmth = std.holding_qty("WARMTH")
local wood   = std.holding_qty("WOOD")
local meat   = std.holding_qty("MEAT")
local berries = std.holding_qty("BERRIES")
local cooked = std.holding_qty("COOKED_MEAT")
local jerky  = std.holding_qty("JERKY")
local food   = berries + cooked + jerky

-- 0. Eat: the stomach empties 0.5/hour plus a tenth of what's left.
--    Meals are labor-free, instant and night-legal -- but they do
--    not happen by themselves.
--    Eat what spoils first (berries, then cooked); jerky never rots,
--    so it is the deep pantry; raw meat is the desperate last resort
--    (a one-in-four chance of disease).
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

-- 1. The fire: build it, then keep it fed. Night draws 3 warmth an
--    hour -- bank a stock before dark (hour 17 onward) and keep the
--    fire fed while labor lasts into the evening. MAKE_FIRE and
--    TEND_FIRE are night-legal (darkness only refuses gathering and
--    hunting).
if not fire then
  if wood >= 2 then
    ctx.action.start_process("MAKE_FIRE", camp)
  end
elseif warmth < (std.hour() and std.hour() >= 17 and 12 or 3)
       and wood >= 1 and not std.running_recipe("TEND_FIRE") then
  ctx.action.start_process("TEND_FIRE", fire)
end

-- 2. Cooking: fire + 2 raw meat -> 2 safe food.
if fire and meat >= 2 and food < 4 then
  ctx.action.start_process("COOK_MEAT", fire)
end

-- 3. Everything else is gathering: food first, then wood for the fire.
--    Daylight only -- the dark refuses the work (std.is_night()).
if (food < 4 or wood < 3) and not std.is_night() then
  if not std.running_recipe("GATHER") then
    ctx.action.start_process("GATHER")
  end
end
