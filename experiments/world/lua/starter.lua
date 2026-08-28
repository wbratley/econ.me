-- starter.lua  (BEHAVIOUR) -- the default a player inherits and edits.
--
-- A defensible minimum: keep yourself fed, work your land, sell the surplus.
-- A player who changes nothing still survives -- their edge comes from
-- REWRITING this to specialise, arbitrage, or coordinate. In the proving
-- experiment this exact script is the Farmer (food self-sufficiency + a food
-- seller); a lone entity endowed with a farm and this script survives
-- indefinitely (test_world.py::test_starter_template_survives).
--
-- Vocabulary arrives from the injected tiers (docs/scripting.md): std.* is
-- the engine stdlib, world.* this world's library, pack.* this content
-- pack's play opinions. A script's source is only ever its own logic; a
-- player rewriting from scratch fetches the tiers (get_script_libraries)
-- and keeps, drops, or replaces the opinions their starter leaned on.

local fills   = world.settle_last_orders()
local account = ctx.accounts[1]
local balance = tonumber(account.balance)
local grain_price = std.market_price("GRAIN", 1.0)

-- 1. Farm grain: one FARM_GRAIN per tick (1 LABOR -> 4 GRAIN), if you own a
--    FARM, hold FARMING, have LABOR, and aren't already running one.
local farm_id = std.facility_parcel("FARM")
if farm_id and std.has_unlock("FARMING")
   and std.holding_qty("LABOR") >= 1 and not std.running_recipe("FARM_GRAIN") then
  ctx.action.start_process("FARM_GRAIN", farm_id)
end

-- 2. Sell grain beyond a small pantry. (FARM_GRAIN yields 4, you eat 1, so a
--    steady surplus flows to the food buyers.)
pack.sell_surplus("GRAIN", 3, 1.0, account.id, fills)

-- 3. Buy food only if the pantry ran low (the fallback a non-farming player
--    relies on; a working farmer's own output keeps this a no-op).
balance = pack.buy_food(account.id, balance, grain_price, 3)
