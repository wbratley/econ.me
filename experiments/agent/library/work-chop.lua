-- ===== BLOCK work.chop | authored:the rate repair (the production-rate cap, runs 84-88) | slot=provisioning | needs=axe =====
--     The rate repair. The cap's anatomy, on record: the market law
--     liquidates the stock (flow diagnosis, run 88) AND the gather
--     arithmetic cannot refill it (0.6 logs/roll expected vs the
--     axe's 3 CERTAIN logs/hour -- the rate diagnosis, the A/B). Chop
--     was never a bad behavior; it was a behavior waiting for its
--     context gate. The gate, by the wood arithmetic: chop when the
--     axe is in hand, the body is fed (food-secure -- a chop trip
--     spends food-time), and the wood bank sits below the line the
--     family needs (fire + the spawn gate + the heir's inheritance).
--     Then back to food. The context gate is the block's value; the
--     5x rate is its point.
local axe_in_hand = std.holding_qty("AXE") >= 1
-- tightened (the first cut spent food-time unaffordably: HUNGER x7
-- vs x3 — the food floor is the guard): chop only on a REAL surplus
-- (8+ food), only while the wood bank is genuinely low (< 6), and
-- only outside autumn (the famine bridge's own season -- autumn
-- belongs to the pantry, never the axe)
local food_secure = (berries + apples + cooked + jerky) >= 8
                  and std.holding_qty("SATIETY") > 5
local bank_line = 6
                  + (C and C.season == "SPRING" and 2 or 0)
                  + (C and C.season == "SUMMER" and 2 or 0)
local not_autumn = C == nil or C.season ~= "AUTUMN"
if axe_in_hand and food_secure and not_autumn and wood < bank_line
   and std.is_day() and not std.running_recipe("CHOP_WOOD")
   and not std.running_recipe("GATHER")
   and not std.running_recipe("GATHER_BAG") then
  ctx.action.start_process("CHOP_WOOD")
end
-- ===== END work.chop =====
