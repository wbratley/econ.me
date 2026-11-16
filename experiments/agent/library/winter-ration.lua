-- ===== BLOCK winter.ration | authored:winter (block-library spec, route a) | slot=survival =====
--     Winter consumption discipline: the roof over the fire. The
--     starter sleeps sheltered only when NO fire is lit (fire-first,
--     wood-hungry); winter nights burn the larder that way. Run-78's
--     line-2 proved the alternative with its body: SLEEP_SHELTERED
--     13.1% of acts vs 2.3/3.8% for fire-sleeping siblings -- the
--     deep optimum moved its nights OFF the fire and onto the roof,
--     and nearly maxed the 36-day probe. In WINTER, with a shelter
--     standing, sleep sheltered as soon as rest runs low -- the wood
--     not burned tonight is tomorrow's warmth.
local C = ctx.clock
if C and C.season == "WINTER" and S.shelter and place == home
   and std.is_night() and rest < 8
   and not std.running_recipe("SLEEP_SHELTERED")
   and not std.running_recipe("SLEEP_BY_FIRE") then
  ctx.action.start_process("SLEEP_SHELTERED")
end
-- ===== END winter.ration =====
