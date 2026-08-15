-- smith.lua  (BEHAVIOUR) -- smelts IRON, buys ORE + food.
--
-- The tech-gated, multi-stage node: buys ORE from the Miner, smelts it
-- (SMELTING + a FORGE), sells IRON. This is the link that proves the
-- substrate -- production crosses a market (ORE in, IRON out) and is gated
-- by a tech unlock a validator/enactment layer would control.
--
-- Buying ORE and smelting it in the SAME tick works through the engine's
-- input-short retry: the smelt intent fails first (no ORE in hand -- the buy
-- hasn't cleared yet), the auction clears the buy, then the smelt is retried
-- and succeeds. One smelt per tick, same as the farmer's one farm.

local fills   = world.settle_last_orders()
local account = ctx.accounts[1]
local balance = tonumber(account.balance)
local ore_price   = std.market_price("ORE", 2.0)
local grain_price = std.market_price("GRAIN", 1.0)

-- 1. Buy ore up to the 2 a smelt needs (each smelt consumes exactly 2).
local ore = std.holding_qty("ORE")
local want_ore = 2 - ore
if want_ore > 0.01 and balance > 0 then
  local bid = math.min(ore_price * 2, balance / want_ore)
  if bid > 0 then
    local qty = math.min(want_ore, balance / bid)
    if qty > 0.01 then
      ctx.action.place_order("ORE", "buy", std.amount_str(qty),
                              std.amount_str(bid), account.id, 35)
      balance = balance - qty * bid
    end
  end
end

-- 2. Smelt iron: needs a FORGE, SMELTING (a world-scope physics unlock),
--    >=2 ORE, and 1 LABOR. Queued before the buy clears -> retried after.
local forge_id = std.facility_parcel("FORGE")
if forge_id and std.has_unlock("SMELTING")
   and std.holding_qty("ORE") >= 2 and std.holding_qty("LABOR") >= 1
   and not std.running_recipe("SMELT_IRON") then
  ctx.action.start_process("SMELT_IRON", forge_id, 20)
end

-- 3. Sell iron (no live buyer in the proving cast -- it accumulates; a
--    toolmaker/steelmaker is a natural Phase 1 addition to close the loop).
sell_surplus("IRON", 0, 5.0, account.id, fills)

-- 4. Buy food.
balance = buy_food(account.id, balance, grain_price, 3)
