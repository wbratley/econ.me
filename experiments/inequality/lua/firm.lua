-- Firm: buys raw labor on the open market, self-converts a slice of it into
-- skilled labor (it keeps a standing agronomist, seeded at genesis), farms
-- its land, keeps its tool stock topped up, makes clothes for sale, and
-- periodically pushes to unlock the better production method.
--
-- A firm is the one agent here that can price labor from first principles:
-- it knows its own recipes, so it knows exactly what an extra unit of labor
-- would produce and what that output sells for. It bids that marginal
-- revenue product -- and because different uses of labor are worth
-- different amounts, it bids a SCHEDULE rather than one number: a separate
-- order per use, each at that use's own value. That is what gives the labor
-- market a genuinely downward-sloping demand curve, and therefore a wage
-- that can settle somewhere instead of drifting (the flat "buy 3 units at
-- last price + 10%" it replaced had no quantity response to price at all,
-- so no amount of cheap labor ever induced anyone to hire more of it).

local fills = settle_last_orders()

local account = ctx.accounts[1]
local balance = tonumber(account.balance)
local field_id = farm_parcel()
local unlocked_agronomy = has_unlock("AGRONOMY")
local labor = holding_qty("LABOR")

local food_price = market_price("FOOD", 3)
local clothes_price = market_price("CLOTHES", 8)
local tools_price = market_price("TOOLS", 20)
local farm_yield = unlocked_agronomy and FOOD_PER_FARM_TOOLED or FOOD_PER_FARM_HAND

-- Firms are not identical: this is seeded per firm at genesis. Without it
-- every firm quotes the same number for the same tranche and the auction
-- rations them by whose order was created first, so the same firm wins the
-- scarce labor every tick forever -- a simulation artifact dressed up as a
-- competitive outcome.
local bid_factor = tonumber(ctx.state.bid_factor) or 1.0

-- Persistent research-push timer: every 20 ticks, if enough LABOR is banked
-- and AGRONOMY isn't unlocked yet, spend a tick converting 6 LABOR into 6
-- LABOR-FARM (six back-to-back duration-0 conversions in one script call --
-- all resolve, priority-ordered, before the research/farm intents below
-- them), then spend 5 of it on research and farm with the 1 left over.
local research_timer = (ctx.state.research_timer or 0) + 1
ctx.state.research_timer = research_timer
local research_push = (not unlocked_agronomy) and research_timer >= 20 and labor >= 6

if research_push then
  for i = 1, 6 do
    ctx.action.start_process("WORK_AS_FARMER", nil, 10)
  end
  ctx.action.start_process("RESEARCH_AGRONOMY", nil, 15)
  if field_id and not running_on(field_id) then
    ctx.action.start_process(unlocked_agronomy and "FARM_FOOD_TOOLED" or "FARM_FOOD_HAND", field_id, 20)
  end
  ctx.state.research_timer = 0
else
  local reserved = 0
  if labor >= 1 and field_id then
    ctx.action.start_process("WORK_AS_FARMER", nil, 10)
    reserved = reserved + 1
    if not running_on(field_id) then
      ctx.action.start_process(unlocked_agronomy and "FARM_FOOD_TOOLED" or "FARM_FOOD_HAND", field_id, 20)
    end
  end
  if labor - reserved >= 3 and holding_qty("TOOLS") < 3 then
    ctx.action.start_process("CRAFT_TOOLS", nil, 25)
    reserved = reserved + 3
  end
  if labor - reserved >= 2 then
    ctx.action.start_process("MAKE_CLOTHES", nil, 26)
  end
end

-- Sell surplus output at cost, adapting downward if it doesn't move.
--
-- Marginal cost is the labor that went into it, valued at the going wage --
-- a real anchor, unlike marking down the last trade. Output is perishable,
-- so an ask that keeps failing to clear is worth conceding on: the bounded
-- multiplier in concede() lets the firm cut below cost rather than watch the
-- stock rot, but cannot compound its way to zero.
local food = holding_qty("FOOD")
if food > 0.01 then
  local unit_cost = market_price("LABOR", 5) / farm_yield
  ctx.action.place_order("FOOD", "sell", amount_str(food),
                          amount_str(quote(unit_cost, concede(fills, "FOOD"))),
                          account.id, 40)
end
local clothes = holding_qty("CLOTHES")
if clothes > 0.01 then
  local unit_cost = market_price("LABOR", 5) / CLOTHES_PER_LABOR
  ctx.action.place_order("CLOTHES", "sell", amount_str(clothes),
                          amount_str(quote(unit_cost, concede(fills, "CLOTHES"))),
                          account.id, 41)
end

-- Buy labor for next tick's operations, as a demand schedule: one order per
-- use of labor, priced at what that use is actually worth.
--
-- No fill-feedback adaptation on this side, deliberately. Marginal revenue
-- product IS the firm's true reservation price, and the auction is
-- uniform-price, so quoting it honestly never means overpaying. Adapting on
-- fills here would be actively wrong: at any real equilibrium the firm's
-- low-value tranches are SUPPOSED to go unfilled, and treating that as a
-- signal to bid higher would walk the firm straight back into paying more
-- for labor than the goods it makes are worth.
local tranches = {}

if field_id then
  -- One field runs one process at a time, so exactly one unit of labor a
  -- tick is worth the full farm yield to this firm. Tooled farming also
  -- burns a little of the tool stock.
  local wear = unlocked_agronomy and (0.02 * tools_price) or 0
  tranches[#tranches + 1] = { qty = 1, price = farm_yield * food_price - wear }
end

tranches[#tranches + 1] = { qty = 2, price = CLOTHES_PER_LABOR * clothes_price }

if holding_qty("TOOLS") < 3 then
  tranches[#tranches + 1] = { qty = LABOR_PER_TOOL, price = tools_price / LABOR_PER_TOOL }
end

-- Labor banked toward the next research push. Unlocking AGRONOMY raises the
-- farm yield permanently, which is worth more than a single tick's output,
-- but valuing it that way would have the firm outbid every other use of
-- labor in the economy for as long as it took. Valuing a research hour at
-- exactly a farm hour is the conservative floor on its worth and keeps the
-- push competing on equal terms with ordinary production.
if not unlocked_agronomy and research_timer >= 15 then
  local shortfall = 6 - labor
  if shortfall > 0 then
    tranches[#tranches + 1] = { qty = shortfall, price = farm_yield * food_price }
  end
end

table.sort(tranches, function(a, b) return a.price > b.price end)

local spend_left = balance
for _, t in ipairs(tranches) do
  local price = t.price * bid_factor
  if price >= 0.01 then
    local qty = math.min(t.qty, spend_left / price)
    if qty > 0.01 then
      ctx.action.place_order("LABOR", "buy", amount_str(qty), amount_str(price),
                              account.id, 45)
      spend_left = spend_left - qty * price
    end
  end
end
