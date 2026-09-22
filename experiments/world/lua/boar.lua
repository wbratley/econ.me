-- boar.lua  (BEHAVIOUR) -- dangerous prey.
--
-- A boar is the larder's guarded shelf (P5): rich -- born carrying
-- its own carcass (6 meat, a double pelt), so a kill pays ~9 meat
-- where a wolf pays 4 -- and DANGEROUS: tusk 4 against hide 2, a
-- 60% answer for 2 a landed tusk. The hunt is a night errand (combat
-- refuses the day), and the boar does not flee it. It is not a
-- predator: it never walks to where the people sleep -- the wolves
-- keep the deep forest and the raids; the boar keeps the thicket,
-- grubbing the same ground the houses gather on. What comes at it
-- here, it answers -- and it remembers, while the foe stands on its
-- ground. The torch that walks you in lights the duel; the spear
-- makes it even; the bow never meets a tusk -- the bow hunts by day.
--
-- The program is small on purpose: a grazer's day (grub the thicket,
-- eat the browse), a body's night (pace, stay warm), and a feud.
-- Omnivore by holdings: berries and apples it rooted, carrion when
-- the forest drops some (the born CARNIVORE gut wrings meat dry --
-- the wolf's stomach, and it eats like one when it has it).

local S = ctx.state

local den = "THICKET"   -- the thicket's edge: the larder's guarded shelf
local river = "RIVER"   -- the tap: the river road runs past the thicket

local hunger  = std.holding_qty("HUNGER")
local satiety = std.holding_qty("SATIETY")
local warmth  = std.holding_qty("WARMTH")
local berries = std.holding_qty("BERRIES")
local apples  = std.holding_qty("APPLES")
local meat    = std.holding_qty("MEAT")
local water   = std.holding_qty("WATER")
local rest    = std.holding_qty("REST")

-- The feud: answer tooth for tusk, and remember who while they stand
-- on this ground. A bounced attack (the foe dead, fled, or day-broke)
-- settles the score -- the boar goes back to its browse. It never
-- hunts anyone: only the provoked bite, and only the provoker.
if std.is_night() then
  for _, e in ipairs(ctx.events or {}) do
    if e.type == "combat" and e.target_id == ctx.entity.id
       and e.entity_id and e.entity_id ~= ctx.entity.id
       and (e.hit or e.deterred) then
      S.foe = e.entity_id
    end
    if e.type == "combat" and e.entity_id == ctx.entity.id
       and e.status == "rejected" then
      S.foe = nil
    end
  end
  if S.foe then
    ctx.action.attack(S.foe)
  elseif rest < 4 and not std.running_recipe("SLEEP_DEN") then
    -- A grazer's night is a den's night (P4): sleep pays the body's
    -- other clock, and the thicket is home. The feud outranks sleep
    -- -- provoked, the boar stands its ground all night and pays
    -- the morning for it.
    ctx.action.start_process("SLEEP_DEN")
  end
end

-- The meal: browse first (rooted this day), carrion when the forest
-- drops some. Meat on hand is the trigger, not hunger -- a kill goes
-- down while it is still warm (the gut is the better larder).
if satiety < 6 then
  if berries >= 2 then
    ctx.action.start_process("EAT_BERRIES")
  elseif apples >= 2 then
    ctx.action.start_process("EAT_APPLES")
  elseif meat >= 1 then
    ctx.action.start_process("EAT_CARRION")
  end
end

-- The graze: grub the thicket by day (the same loot table the houses
-- work -- one roll an hour; whatever turns up, roots and all). The
-- boar's one errand is water (P3): the river road runs past the
-- thicket, two hours -- it drinks where the valley drinks, and it
-- never needs the fire-ground to do it. It is still not a predator:
-- the walk is a walk, the browse is the business.
if water < 1 and not std.is_night() and ctx.entity.place ~= river then
  ctx.action.travel(river)
elseif ctx.entity.place == river and water < 2 then
  ctx.action.start_process("DRINK")
elseif not std.is_night() and ctx.entity.place ~= den
    and water >= 1 then
  -- The walk home. Run-44's autopsy: the drink-run had no return --
  -- a boar topped its cup at the bank and then stood there, PACE and
  -- SLEEP on the gravel, for the rest of its life. Boar I drank at
  -- t90/100/109/118 with zero travels after the outbound walk, and
  -- starved at t128 two hours from a thicket full of browse. A
  -- grazer's business is the browse: water carried, hooves home.
  ctx.action.travel(den)
elseif not std.is_night() and hunger > 1
    and not std.running_recipe("GATHER")
    and ctx.entity.place == den then
  ctx.action.start_process("GATHER")
end

-- The body: a moving animal stays warm (a grazer paces its pen of
-- thicket all night, warm as any wolf).
if warmth < 2 then
  ctx.action.start_process("PACE")
end
