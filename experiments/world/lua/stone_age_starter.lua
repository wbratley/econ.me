-- stone_age_starter.lua  (BEHAVIOUR) -- the default a player inherits.
--
-- A hand-to-mouth loop with a commute (S4: the world has places, and
-- they are hours apart): keep the COMMONS fire fed and sleep by it
-- through the dark (P4: warming is a seat, resting is a watch -- the
-- fire pays both while it burns), EAT when the stomach runs low
-- (eating is your decision now -- the engine no longer chews for you),
-- gather whatever the thicket offers, and WALK between them -- an hour
-- each way, the road exposed place by place. It survives -- barely.
-- Beyond the treadmill it owns exactly ONE rung of capital, the spear
-- (run 47: the world never made meat -- the pot sat cold and the post
-- bid 5.00 into an empty market all day; protein is the floor's
-- business now) -- and since P1 the rung is a LADDER of two rungs,
-- taken in certainty order (run 56, the run-55 lesson: the floor gated
-- the whole climb on a yarn drop the gather table pays one roll in
-- twenty, and the houses starved beside a world of meat -- zero
-- studies, zero spears, six days): first the STUDY, whose stones the
-- scrape gives certain (dig two, strike two hours, done -- the first
-- morning the stomach can spare), then the haft, whose yarn is the
-- lottery it always was, taken the day it turns up. Protein before
-- the spear rides the drink errand: fish at the bank (certain-ish,
-- and nothing there hunts you); with a spear in hand the house walks
-- the deep forest's table when the meat shelf thins. Everything else
-- -- bag, axe, bow, shelter, clothes, even its own fire -- stays the
-- player's invention: the floor remains POOR, and the spear is the
-- floor's because without it the protein walk is a 55%-nothing gamble
-- the floor does not take.
-- It still spends nearly every tick on the next meal or the next
-- log: that is the point. A player who changes nothing stays on the
-- treadmill; every further escape -- tools, clothes, shelter, trade
-- -- is theirs to add on top of the hunt.
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
local spear  = std.holding_qty("SPEAR")    -- the hunt's key (held, never spent)
local comfort = std.holding_qty("COMFORT")  -- ease banked (run 54 P2)
local squalor = std.holding_qty("SQUALOR")  -- the unloved hours' memory
local flint  = std.holding_qty("FLINT")
local yarn   = std.holding_qty("YARN")
local protein = meat + cooked + jerky      -- the meat-family shelf (raw MEAT
                                            -- rots fastest; the walk pays in it)
local eggs   = std.holding_qty("EGGS")     -- the flock's wage (keeps near a week)

-- 0. Eat: the stomach empties 0.5/hour plus a tenth of what's left.
--    Meals are labor-free, instant, night-legal and place-free -- but
--    they do not happen by themselves. Eat what spoils first (berries,
--    then apples, then cooked); jerky never rots, so it is the deep
--    pantry; the pot boils a basket no single meal covers (run 46: a
--    house starved holding 0.8+0.7+0.6). Raw meat is the desperate
--    LAST resort (a one-in-four chance of a whole point of disease
--    against a 2.5 kill line) -- the bar is real starvation (HUNGER
--    riding 5+, a day of missed draws), not an empty stomach: the
--    catch walks home and cooks (COOK_MEAT, any lit fire, night-legal).
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
  elseif berries >= 0.5 and apples >= 0.5 and meat >= 0.5
       and std.has_unlock("POTTERY") then
    ctx.action.start_process("FORAGE_POT")
  elseif berries >= 0.5 and apples >= 0.5 and eggs >= 0.5
       and std.has_unlock("POTTERY") then
    -- the rancher's pot: an egg takes the meat leg's place (run 47:
    -- a flock could not out-climb the stomach eaten whole, but half
    -- an egg boils as thick as a hunted meal)
    ctx.action.start_process("FORAGE_POT_EGGS")
  elseif meat >= 1 and std.holding_qty("HUNGER") > 5 then
    -- Desperation, priced honestly: raw is for a body already missing
    -- its draws (HUNGER 5+ = a day's worth) -- a missed hour costs 1.0
    -- hunger against a 15 kill line, so a merely-empty stomach WALKS
    -- ITS CATCH HOME AND COOKS IT. One raw meal in four grants a
    -- whole point of DISEASE against a 2.5 kill line: a floor that ate
    -- its catch raw at the river died fed, of the sickness (run 56's
    -- soak, twice).
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
elseif place == river and protein < 3 and std.hour() < 15 then
  -- Standing at the bank with a thin meat shelf: fish it (run 47's
  -- lesson -- the world's meat never existed; the drink errand
  -- doubles as the catch, certain-ish, and nothing here hunts YOU).
  -- This is the floor's protein rung BEFORE the spear (run 55: the
  -- houses gathered to mouth and starved beside a fishable bank --
  -- the gateless cousin was always there; the floor never walked
  -- it). The deliberate walk is the day plan's, below -- ONE walk
  -- decision per day, not a race of errands (run 56's soak: a floor
  -- whose errands each pulled their own road zig-zagged river,
  -- scrape, thicket in a day and came home woodless -- the fire died
  -- at dusk and the packs walked in).
  ctx.action.start_process("FISH")
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
-- P2: EASE is a want with a bite now -- an unloved hour accrues
-- squalor, and squalor shrinks the daylight ration until whole-hour
-- work refuses. The cure is the same seat: fire hours pay comfort
-- (+0.5, sleep +0.5, a bed +1), so the floor sits for ease exactly
-- as it sits for warmth -- an hour of the afternoon when the ease
-- runs dry. The seat is free; the hour is not.
if (std.is_night() or warmth < 4 or comfort < 0.1) and #fires > 0
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

-- 2. Cooking: fire + 2 raw meat -> 2 safe food. Raw MEAT rots a
--    third an hour -- a hunt not cooked on arrival is half-wasted,
--    so the gate is the cooked shelf, not hunger. The commons fire
--    cooks for anyone at the clearing (lit -- the starter stokes
--    first; coals do not cook).
if place == home and meat >= 2 and cooked < 6 then
  ctx.action.start_process("COOK_MEAT")
end

-- 2a was folded into the day plan (block 3): the study walk is a
--    DAY decision, taken in priority order against the fish errand
--    and the gather run -- see 3.
local knapping = std.has_unlock("KNAPPING")

-- 2b. The spear rung (run 47's lever: meat must exist, and the floor
--     must make it; run 54 P1: the haft wants the KNAPPING rank,
--     studied at the scrape above; run 56: the haft's YARN is the
--     lottery it always was -- one roll in twenty at the thicket, or
--     the post's shelf -- and the floor takes it the day it turns
--     up, not before: the study already happened). An afternoon's
--     hafting when the woodpile can spare the two logs -- the floor's
--     one piece of capital; the rest of the hunt ladder (bag, axe,
--     bow, traps, and every other rank) stays the player's invention.
--     Early enough that three honest hours still end in light; the
--     walk home from the scrape is part of the errand.
if spear < 1 and knapping and flint >= 1 and wood >= 4 and yarn >= 1
   and not std.is_night() and std.hour() < 15 then
  if place ~= home then
    ctx.action.travel(home)
  else
    ctx.action.start_process("MAKE_SPEAR")
  end
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
--    walk home is priced from WHERE THE HOUSE STANDS: the forest and
--    the bank are two hours out, not one -- a house that leaves late
--    freezes one road from its fire (run 37's twins). Same bar as
--    always: no walk may LAND in the dark.
local forest = "FOREST"           -- the spear game: the deep wood's table
local walk_home = world.distance_ticks(place or home, home) or 1
local dusk_closing = std.hour() + walk_home >= 19
-- The protein walk: a speared house hunts the deep forest when the
-- meat shelf thins (protein < 3: one landed hunt retires the errand)
-- and the day is young enough for the whole circuit -- out by the
-- valley road, two honest hours of hunt, home before the dark; the
-- window closes at hour 13 (a late start is a dark road home). The
-- berry flood pauses it: a full vegetarian shelf does not need the
-- spear. Bare-handed the floor stays home -- the thicket's table
-- feeds better than a hungry gamble.
local hunting = spear >= 1 and protein < 3 and food < 6
   and std.hour() < 13 and not std.running_recipe("HUNT_SPEAR")
if not std.is_night() then
  if hunting then
    if place ~= forest then
      ctx.action.travel(forest)
    else
      ctx.action.start_process("HUNT_SPEAR")
    end
  -- 3a. The study morning (run 54 P1's ladder, run 56's order):
  --     knapping is studied, not born, and the STUDY is the certain
  --     rung -- stones dug at the scrape, two hours, done. Run 55's
  --     floor never opened the ladder (it demanded a one-in-twenty
  --     yarn drop banked BEFORE the first walk); this floor takes
  --     the rung the first morning the stomach can spare it and the
  --     night's fire is already paid (wood >= 4: the circuit costs a
  --     whole day -- a floor that reaches dusk thin loses the fire,
  --     and the fire's warmth is the wall the wolves respect). Out
  --     by mid-morning, home before dark; the haft (3b) waits for yarn.
  elseif not knapping and spear < 1 and (food + satiety) >= 4
     and wood >= 4 and can_walk and std.hour() < 11 then
    if place == "FLINT" then
      if flint >= 3 then
        ctx.action.start_process("STUDY_KNAPPING")
      else
        ctx.action.start_process("DIG_FLINT")
      end
    else
      ctx.action.travel("FLINT")
    end
  -- 2c. The feast (run 54 P2): comfort is the one want worth being
  --     SAFE for, and the feast is its one bulk buy. When ease has run
  --     dry (an unloved hour accrues squalor, and squalor shrinks the
  --     ration) and the surplus shelf is FAT (eight strips is two days
  --     of dense food -- a bank, not a meal), spend an afternoon at
  --     the fire: four strips and a log into five points of ease. A
  --     squalid body cannot start it -- the throttle refuses the
  --     labor; the seat is the cure, the feast is the reward.
  elseif comfort < 0.1 and squalor == 0 and jerky > 8 and #fires > 0
     and place == home and std.hour() < 15
     and not std.running_recipe("FEAST") then
    ctx.action.start_process("FEAST")
  -- 3b. The fish errand (run 55's lesson: the houses gathered to
  --     mouth and starved beside a fishable bank): before the spear
  --     exists -- and while the vegetarian shelf is thin -- the river
  --     is the protein rung, and it costs a morning: walk, catch,
  --     cook at home. The same wood-first law as the study morning
  --     (wood >= 4 -- the night banked before the day is spent): the
  --     errand yields to the gather run once the fire is unpaid.
  --     0a's at-river branch sets the catch itself.
  elseif protein < 2 and food < 6 and wood >= 4
     and std.hour() < 13 then
    if place ~= river then
      ctx.action.travel(river)
    end
  elseif (food < 8 or wood < 6) and not dusk_closing
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
