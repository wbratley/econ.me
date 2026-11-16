-- ===== BLOCK winter.stockpile | authored:winter (block-library spec, route a) | slot=provisioning | provides=S.stock =====
--     Autumn surplus routing: the pantry fills BEFORE the snow. The
--     starter gathers to hand-to-mouth floors (a day of food, a
--     night of wood); this block raises the floors through AUTUMN
--     and late SUMMER, scaled to the season's phase -- the later the
--     season-day, the deeper the pantry target. Winter is not fed
--     here (that is RATION's discipline): this is the squirrel, not
--     the miser.
local C = ctx.clock
if C and (C.season == "AUTUMN" or (C.season == "SUMMER" and C.season_day >= 5)) then
  local phase = C.season == "AUTUMN" and C.season_day or 0
  local food_total = berries + apples + cooked + jerky
  local food_target = 10 + 2 * phase
  local wood_target = 8 + phase
  if std.is_day() and place == home
     and not std.running_recipe("GATHER")
     and not std.running_recipe("GATHER_BAG")
     and not std.running_recipe("FISH")
     and not std.running_recipe("HUNT_SPEAR") then
    if food_total < food_target or wood < wood_target then
      S.stock = true
      ctx.action.start_process(bag >= 1 and "GATHER_BAG" or "GATHER")
    end
  end
end
-- ===== END winter.stockpile =====
