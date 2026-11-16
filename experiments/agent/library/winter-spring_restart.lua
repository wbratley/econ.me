-- ===== BLOCK winter.spring_restart | authored:winter (block-library spec, route a) | slot=recovery | needs=S.last_season | provides=S.last_season,S.spring_day =====
--     Post-winter recovery: the first days of spring decide whether
--     the winter's survivor is a survivor or a corpse on a delay --
--     the larder is ash, the purse is thin, and the body is tired.
--     This block latches the season turn (S.last_season, updated
--     every tick it differs) and, for three days after winter breaks
--     (S.spring_day), commits daylight to the thicket: food first,
--     wood behind it -- the fire must be fed before anything else
--     can be. S.last_season is self-provided on first run (nil-safe:
--     the latch writes before the read matters).
local C = ctx.clock
if C then
  if S.last_season ~= C.season then
    S.last_season = C.season
    if C.season == "SPRING" then
      S.spring_day = std.day()
    end
  end
  if S.spring_day and std.day() - S.spring_day <= 3
     and std.is_day() and place == home
     and not std.running_recipe("GATHER")
     and not std.running_recipe("GATHER_BAG")
     and not std.running_recipe("FISH")
     and not std.running_recipe("HUNT_SPEAR") then
    local food_total = berries + apples + cooked + jerky
    if food_total < 6 or wood < 4 then
      ctx.action.start_process(bag >= 1 and "GATHER_BAG" or "GATHER")
    end
  end
end
-- ===== END winter.spring_restart =====
