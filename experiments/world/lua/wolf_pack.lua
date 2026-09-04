-- wolf_pack.lua  (BEHAVIOUR) -- the predator's program.
--
-- A wolf is a creature with the same physics as everyone: hunger is
-- why it hunts, cold is why it paces, meat is what it eats -- raw, and
-- its iron stomach makes a proper meal of it (EAT_CARRION: the born
-- CARNIVORE trait -- a kill wrung dry, jerky-dense, never
-- sickening). It hears
-- the world (the witness feed): at night, the loudest speaker is the
-- easiest prey -- speech carries. Silence does not make you safe, only
-- hard to find: a starving pack prowls anyway (attack(nil) -- the
-- engine picks the noisiest of the night, else anyone standing close).
--
-- The pack RANGES (run 26's census: denned wolves whose houses slept
-- out of reach starved -- the map made sit-and-wait a death sentence).
-- By day it works the forest game (HUNT, the same table the houses
-- hunt); by night, a hungry pack walks: three hours of road to the
-- fire-ground where the people sleep, and home again by dawn. Players
-- are not the pack's only food -- the forest feeds it -- they are the
-- rich exception a hungry night goes looking for.
--
-- The program never fights fire: a lit hearth turns a pack at the door
-- (combat rules do that). It attacks what it can find up close, eats
-- what it caught, and stays warm by moving.

local S = ctx.state

local home  = "FOREST"   -- the range: dens and the day's game
local prowl = "HEARTH"   -- the raid: where the people sleep

local hunger  = std.holding_qty("HUNGER")
local satiety = std.holding_qty("SATIETY")
local meat    = std.holding_qty("MEAT")
local warmth  = std.holding_qty("WARMTH")

-- Ears: tonight's says name tonight's prey. Remember the loudest;
-- the memory fades at dawn. A target that cannot be fought (the
-- refused attacks in the feed) is dropped -- a corpse is not prey.
if std.is_night() then
  S.night_says = S.night_says or {}
  for _, e in ipairs(ctx.events or {}) do
    if e.type == "say" and e.entity_id and e.entity_id ~= ctx.entity.id
       and e.status ~= "rejected" then
      S.night_says[e.entity_id] = (S.night_says[e.entity_id] or 0) + 1
    end
    if e.type == "combat" and e.entity_id == ctx.entity.id
       and e.status == "rejected" then
      S.prey = nil
      S.night_says = {}
    end
  end
  local best, best_n = nil, -1
  for id, n in pairs(S.night_says) do
    if n > best_n then best, best_n = id, n end
  end
  if S.night_says and next(S.night_says) then
    S.prey = best
  end
else
  S.night_says = {}
  S.prey = nil
end

-- The hunt: by dark, the loud (a starving pack prowls blind -- the
-- bite is up close now, and the map made sure of it); by day, the
-- same game the houses hunt. Hunger moves the pack between them: the
-- raid walk is hours of night road, so the walk IS the prowl -- a
-- refused bite and a journey may share the tick (the road takes
-- hours; a bounced intent costs nothing).
if std.is_night() then
  if hunger > 3 then
    if S.prey then
      ctx.action.attack(S.prey)
    elseif hunger > 8 then
      ctx.action.attack(nil)
    end
    if ctx.entity.place ~= prowl then
      ctx.action.travel(prowl)
    end
  end
else
  -- day: the pack works its range. The game is in the forest and the
  -- dens are there; a fed wolf still walks home -- the fire-ground
  -- by day is a bad bed, and the range keeps being a range by being
  -- walked.
  if ctx.entity.place ~= home then
    ctx.action.travel(home)
  elseif hunger > 2 and not std.running_recipe("HUNT") then
    ctx.action.start_process("HUNT")
  end
end

-- The meal: carrion. A wolf's stomach is iron -- raw is its cooked.
-- And it eats GREEDILY: meat rots a third an hour in the pantry, a
-- full stomach spills only a tenth -- the gut is the better larder,
-- so a kill goes down while it is still warm (hunger is not the
-- trigger; meat on hand is).
if meat >= 1 and satiety < 6 then
  ctx.action.start_process("EAT_CARRION")
end

-- The body: a moving animal stays warm.
if warmth < 2 then
  ctx.action.start_process("PACE")
end
