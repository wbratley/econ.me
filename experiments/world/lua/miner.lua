-- miner.lua  (BEHAVIOUR) -- mines ORE, sells it, buys food.
--
-- The extraction specialist: 1 LABOR + a drawn-down ORE seam -> 2 ORE, sold
-- to the Smith. Food is bought, not grown -- this entity is the demand side
-- of the food market and the supply side of the ore market. The deposit on
-- its parcel depletes only through MINE_ORE and regenerates toward capacity.
-- Vocabulary arrives injected (docs/scripting.md): std.* (engine),
-- world.* (this world's idioms), pack.* (this content pack's play
-- opinions). Source here is only the role's own logic.

local fills   = world.settle_last_orders()
local account = ctx.accounts[1]
local balance = tonumber(account.balance)
local grain_price = std.market_price("GRAIN", 1.0)

-- 1. Mine ore on the deposit parcel (no facility needed -- the deposit binds
--    the recipe to the parcel).
local mine_id = std.deposit_parcel("ORE")
if mine_id and std.holding_qty("LABOR") >= 1 and not std.running_recipe("MINE_ORE") then
  ctx.action.start_process("MINE_ORE", mine_id)
end

-- 2. Sell all ore (labour is free, so the anchor is a nominal price; concede
--    drops it until the Smith bites).
pack.sell_surplus("ORE", 0, 2.0, account.id, fills)

-- 3. Buy food.
balance = pack.buy_food(account.id, balance, grain_price, 3)
