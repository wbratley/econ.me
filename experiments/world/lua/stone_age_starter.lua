-- stone_age_starter.lua  (BEHAVIOUR) -- the default a player inherits.
--
-- A hand-to-mouth loop: keep a fire, feed it logs when the warmth runs
-- low, cook meat when there is any, gather whatever the forest offers.
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

local camp = ctx.parcels[1] and ctx.parcels[1].id
local fire = std.facility_parcel("FIRE")

local warmth = std.holding_qty("WARMTH")
local wood   = std.holding_qty("WOOD")
local meat   = std.holding_qty("MEAT")
local food   = std.holding_qty("BERRIES") + std.holding_qty("COOKED_MEAT")

-- 0. Desperate: nothing to eat but raw meat. Take the risk -- disease is
--    a chance, starvation is a schedule. (EAT_RAW is labor-free.)
if food + std.holding_qty("SATIETY") < 1 and meat >= 1 then
  ctx.action.start_process("EAT_RAW", nil, 20)
end

-- 1. The fire: build it, then keep it fed. A tended fire (~8 warmth a
--    log) covers ~4 ticks; tend when the stock runs low.
if not fire then
  if wood >= 2 then
    ctx.action.start_process("MAKE_FIRE", camp, 20)
  end
elseif warmth < 3 and wood >= 1 and not std.running_recipe("TEND_FIRE") then
  ctx.action.start_process("TEND_FIRE", fire, 20)
end

-- 2. Cooking: fire + 2 raw meat -> 2 safe food.
if fire and meat >= 2 and food < 4 then
  ctx.action.start_process("COOK_MEAT", fire, 20)
end

-- 3. Everything else is gathering: food first, then wood for the fire.
if food < 3 or wood < 3 then
  if not std.running_recipe("GATHER") then
    ctx.action.start_process("GATHER", nil, 20)
  end
end
