-- wolf_pack.lua  (BEHAVIOUR) -- the predator's program.
--
-- A wolf is a creature with the same physics as everyone: hunger is
-- why it hunts, cold is why it paces, meat is what it eats (raw -- its
-- constitution, not ours). It hears the world (the witness feed): at
-- night, the loudest speaker is the easiest prey -- speech carries.
-- Silence does not make you safe, only hard to find: a starving pack
-- prowls anyway (attack(nil) -- the engine picks the noisiest of the
-- night, else anyone).
--
-- The program never fights fire: a lit hearth turns a pack at the door
-- (combat rules do that). It attacks what it can find, eats what it
-- caught, and stays warm by moving.

local S = ctx.state

local hunger  = std.holding_qty("HUNGER")
local satiety = std.holding_qty("SATIETY")
local meat    = std.holding_qty("MEAT")
local warmth  = std.holding_qty("WARMTH")

-- Ears: tonight's says name tonight's prey. Remember the loudest; the
-- memory fades at dawn.
if std.is_night() then
  S.night_says = S.night_says or {}
  for _, e in ipairs(ctx.events or {}) do
    if e.type == "say" and e.entity_id and e.entity_id ~= ctx.entity.id
       and e.status ~= "rejected" then
      S.night_says[e.entity_id] = (S.night_says[e.entity_id] or 0) + 1
    end
  end
  local best, best_n = nil, -1
  for id, n in pairs(S.night_says) do
    if n > best_n then best, best_n = id, n end
  end
  S.prey = best
else
  S.night_says = {}
  S.prey = nil
end

-- The hunt: hungry and dark. Desperation (HUNGER > 8) prowls blind.
if std.is_night() and hunger > 3 then
  if S.prey then
    ctx.action.attack(S.prey)
  elseif hunger > 8 then
    ctx.action.attack(nil)
  end
end

-- The meal: meat, raw. Wolves do not cook.
if satiety < 1 and meat >= 1 then
  ctx.action.start_process("EAT_RAW")
end

-- The body: a moving animal stays warm.
if warmth < 2 then
  ctx.action.start_process("PACE")
end
