-- Person: sells labor (or farms their own land if a smallholder), buys food
-- first then clothes with what's left, and optionally remits voluntary tax
-- to the Treasury. This exact script runs unmodified across every
-- rule-variant of the experiment; only the numbers in ctx.state differ
-- (tax_rate, tax_threshold) -- policy is script, the votable rate is data.
--
-- Prices quoted here are reservation prices, not marks off the last trade
-- (see the prelude for why): what this person will accept for an hour of
-- their labor, and what a meal is worth to them given how hungry they are
-- and what they have in the bank.

local function need_by_code(code)
  for _, n in ipairs(ctx.needs) do
    if n.code == code then return n end
  end
  return nil
end

local fills = settle_last_orders()

local account = ctx.accounts[1]
local balance = tonumber(account.balance)

local food_price = market_price("FOOD", 3)
local labor_price = market_price("LABOR", 5)

-- How much stock this household tries to keep on the shelf. FOOD spoils
-- fast, so a pantry is a real cost, but going into a tick with nothing is
-- what starts the poverty condition -- a few ticks of cover is the trade.
local FOOD_PANTRY = 5
local CLOTHES_STOCK = 1
local FOOD_BUDGET_SHARE = 0.5     -- of ordinary spending
local SHELTER_BUDGET_SHARE = 0.20
local ENERGY_BUDGET_SHARE = 0.10
local CLOTHES_BUDGET_SHARE = 0.15
-- How far above the normal price each bill is worth chasing when it is
-- already unpaid. Well below food's premium of 20: a cold night is worse than
-- an ordinary one and nothing like a hungry one, and pricing them equally
-- would have households bidding rent money away from dinner.
local SHELTER_PREMIUM = 8
local POWER_PREMIUM = 4
local HUNGER_PREMIUM = 20         -- multiple of the normal price a starving
                                  -- person will pay rather than go without

-- 1. Voluntary tax remittance. The ownership invariant means the Treasury
--    can never take this by force -- this is self-assessed compliance, and
--    the baseline rule-variant (tax_rate = 0) makes this a no-op.
local tax_rate = tonumber(ctx.state.tax_rate) or 0
local tax_threshold = tonumber(ctx.state.tax_threshold) or 0
if tax_rate > 0 and balance > tax_threshold then
  local owed = (balance - tax_threshold) * tax_rate
  if owed > 0.01 then
    ctx.action.transfer(account.id, ctx.state.treasury_account_id,
                         string.format("%.4f", owed), "tax", 5)
    balance = balance - owed
  end
end

-- What this household reckons a unit of each good is normally worth, in
-- money: the slice of ordinary spending it allots to that good, divided by
-- the units it actually gets through in a tick. Note what is NOT in it --
-- the market price. That is the whole point: a quote built only from cash
-- and from fixed real consumption gives the price level something to sit
-- on, where a quote built from the last trade only ever tells you where the
-- last trade was. It also makes wealth into purchasing power directly: two
-- people equally hungry but unequally rich quote very different numbers for
-- the same loaf, and the richer one eats.
local spend_rate = balance / PLANNING_HORIZON
local routine_food = SUBSISTENCE_FOOD + FOOD_DECAY * FOOD_PANTRY
local routine_clothes = COMFORT_CLOTHES + CLOTHES_DECAY * CLOTHES_STOCK
local normal_food_price = (spend_rate * FOOD_BUDGET_SHARE) / routine_food
local normal_clothes_price = (spend_rate * CLOTHES_BUDGET_SHARE) / routine_clothes
-- No decay term in the denominator for these two, unlike food and clothes:
-- they decay ENTIRELY, so the routine quantity is just the tick's
-- requirement. Nothing spoils on the shelf because nothing reaches the shelf.
local normal_shelter_price = (spend_rate * SHELTER_BUDGET_SHARE) / SHELTER_PER_TICK
local normal_energy_price = (spend_rate * ENERGY_BUDGET_SHARE) / ENERGY_PER_TICK

-- 2. Work the land, if any (smallholder path): convert 1 LABOR -> 1
--    LABOR-FARM (gated on holding >= 1 SKILL-FARM, checked not consumed),
--    then farm with it the same tick -- duration-0 completions resolve
--    before the priority-20 farm intent, so the conversion's output is
--    already in hand by the time the farm run checks its inputs.
local field_id = farm_parcel()
local skill = holding_qty("SKILL-FARM")
local tooled = has_unlock("AGRONOMY")
local reserved_labor = 0
if field_id and skill >= 1 and holding_qty("LABOR") >= 1 then
  ctx.action.start_process("WORK_AS_FARMER", nil, 10)
  reserved_labor = 1
  if not running_on(field_id) then
    if tooled then
      ctx.action.start_process("FARM_FOOD_TOOLED", field_id, 20)
    else
      ctx.action.start_process("FARM_FOOD_HAND", field_id, 20)
    end
  end
end

-- 3. Sell whatever labor isn't reserved for farming this tick.
--
--    The reservation wage -- the least this person will work for -- is what
--    a day's food costs them, discounted by the fact that some work beats
--    none. Because it is priced off their own normal food price it inherits
--    that number's dependence on their savings: someone with money can turn
--    down a bad offer, someone with nothing cannot and takes what is going.
--    So the poor are the cheapest workers and fill first (eligible sells
--    fill cheapest-first at the auction) -- a real mechanism, and one that
--    incidentally stops every individual quoting an identical price and
--    being rationed purely by whose script happened to run first.
local reservation_wage = normal_food_price * SUBSISTENCE_FOOD * 0.6

local spare_labor = math.max(0, holding_qty("LABOR") - reserved_labor)
if spare_labor > 0.01 then
  ctx.action.place_order("LABOR", "sell", amount_str(spare_labor),
                          amount_str(quote(reservation_wage, concede(fills, "LABOR"))),
                          account.id, 40)
end

-- Skilled labor a smallholder converted but had no free field to use: it
-- decays, and they cannot eat it, so they open at what it is worth to a
-- buyer (the food it would grow) and concede from there if nobody bites.
local spare_skilled = holding_qty("LABOR-FARM")
if spare_skilled > 0.01 then
  local yield = tooled and FOOD_PER_FARM_TOOLED or FOOD_PER_FARM_HAND
  ctx.action.place_order("LABOR-FARM", "sell", amount_str(spare_skilled),
                          amount_str(quote(yield * food_price, concede(fills, "LABOR-FARM"))),
                          account.id, 40)
end

-- 3b. Sell surplus food beyond a small pantry buffer -- a smallholder's own
--     harvest is usually more than their own need; without this the surplus
--     would just sit and rot (FOOD decays) instead of feeding landless
--     neighbours through the market. They open at what the harvest cost
--     them (a tick of labor, valued at the going wage or their own
--     reservation wage, whichever is higher, spread over the expected
--     yield) and concede toward giving it away rather than let it rot.
local spare_food = holding_qty("FOOD") - 2
if spare_food > 0.01 then
  local yield = tooled and FOOD_PER_FARM_TOOLED or FOOD_PER_FARM_HAND
  local unit_cost = math.max(labor_price, reservation_wage) / yield
  ctx.action.place_order("FOOD", "sell", amount_str(spare_food),
                          amount_str(quote(unit_cost, concede(fills, "FOOD"))),
                          account.id, 41)
end

-- 4. Buy food first (essential, priority 0), then clothes with what's left
--    (secondary, priority 1) -- the needs hierarchy expressed as a real
--    budget-allocation choice under scarcity, not just processing order.
--
--    Food is bought in two tiers, and the split is what gives this
--    household a demand CURVE rather than a single take-it-or-leave-it
--    point. Tonight's meal is nearly price-insensitive and worth digging
--    into savings for; next week's pantry is worth stocking only at a
--    discount. Without the second tier demand is a fixed quantity at any
--    price, and a market in surplus has nothing to stop it falling to the
--    floor -- which is exactly what it did.
local budget = balance
local held_food = holding_qty("FOOD")
local hunger = need_by_code("HUNGER")
if hunger and budget > 0 then
  local satisfaction = tonumber(hunger.satisfaction)
  local urgency = 1 - satisfaction
  local shortfall = tonumber(hunger.quantity_per_tick) * urgency

  -- Tier 1: what the pantry cannot cover of tonight's meal. Going without
  -- starts the poverty condition, so the quote climbs to a large multiple
  -- of the normal price -- bounded only by what the purse can actually
  -- settle. Quoting honestly is safe: the auction is uniform-price, so the
  -- limit decides whether you eat, not what you pay.
  local eat_now = math.max(0, shortfall - held_food)
  if eat_now > 0.01 then
    local price = quote(normal_food_price * (1 + HUNGER_PREMIUM * urgency), 1,
                        nil, budget / eat_now)
    local qty = math.min(eat_now, budget / price)
    if qty > 0.01 then
      ctx.action.place_order("FOOD", "buy", amount_str(qty),
                              amount_str(price), account.id, 30)
      budget = budget - qty * price
    end
  end

  -- Tier 2: restocking, out of ordinary income rather than savings, and
  -- only below what the household reckons food normally costs.
  local pantry_gap = FOOD_PANTRY - held_food - eat_now
  if pantry_gap > 0.01 and budget > 0 then
    local price = quote(normal_food_price * 0.6, 1, nil, budget / pantry_gap)
    local qty = math.min(pantry_gap, budget / price)
    if qty > 0.01 then
      ctx.action.place_order("FOOD", "buy", amount_str(qty),
                              amount_str(price), account.id, 31)
      budget = budget - qty * price
    end
  end
end

-- Rent and the electricity bill, bought after food and before clothes to
-- match the need priorities in scenario.py, and out of whatever food left
-- behind.
--
-- These are BILLS, not shopping, and the mechanism that makes them so is
-- total decay: there is no pantry to draw on and no stock to build up, so the
-- household buys exactly this tick's requirement or does without it, and the
-- same amount falls due again next tick regardless. A single tier, therefore
-- -- the two-tier split that gives food its demand curve has nothing to
-- describe here, because "stock up on cheap rent" is not a thing a tenant can
-- do.
--
-- The consequence of the ordering is that a squeezed household goes cold
-- before it goes hungry. That is deliberate, and it is where the poverty trap
-- lives: COND-EXPOSED and COND-COLD both cut what you can earn, so a missed
-- bill lowers next tick's income, which makes the next bill harder.
local function buy_bill(need_code, symbol, normal_price, premium, prio)
  local need = need_by_code(need_code)
  if not (need and budget > 0) then return end
  local urgency = 1 - tonumber(need.satisfaction)
  local due = tonumber(need.quantity_per_tick) - holding_qty(symbol)
  if due <= 0.01 then return end
  local price = quote(normal_price * (1 + premium * urgency), 1, nil, budget / due)
  local qty = math.min(due, budget / price)
  if qty > 0.01 then
    ctx.action.place_order(symbol, "buy", amount_str(qty), amount_str(price),
                            account.id, prio)
    budget = budget - qty * price
  end
end

buy_bill("SHELTER", "SHELTER", normal_shelter_price, SHELTER_PREMIUM, 32)
buy_bill("POWER", "ENERGY", normal_energy_price, POWER_PREMIUM, 33)

-- Clothes are discretionary: quoted at the normal price with none of
-- food's urgency term, and only out of what food left behind. A hungry
-- person spends nothing here, which is the needs hierarchy doing real work
-- rather than just ordering the two loops.
local comfort = need_by_code("COMFORT")
if comfort and budget > 0 then
  local want = CLOTHES_STOCK - holding_qty("CLOTHES")
  if want > 0.01 then
    local price = quote(normal_clothes_price, 1, nil, budget / want)
    local qty = math.min(want, budget / price)
    if qty > 0.01 then
      ctx.action.place_order("CLOTHES", "buy", amount_str(qty),
                              amount_str(price), account.id, 35)
    end
  end
end
