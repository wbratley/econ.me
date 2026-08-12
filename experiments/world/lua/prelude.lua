-- Shared prelude, prepended verbatim to every role script by scenario.py.
-- The Lua sandbox has no `require`, so shared helpers live here rather than
-- in an imported module. Kept deliberately small: this is a substrate, not a
-- calibrated economy, so the pricing is simple reservation-and-adapt rather
-- than the marginal-reproduct machinery of experiments/inequality.

-- Lookup helpers -----------------------------------------------------------
local function holding_qty(symbol)
  for _, h in ipairs(ctx.holdings) do
    if h.symbol == symbol then return tonumber(h.quantity) end
  end
  return 0
end

local function market_price(symbol, fallback)
  local p = ctx.query.market_price(symbol)
  if p then
    p = tonumber(p)
    if p and p > 0 then return p end
  end
  return fallback
end

local function has_unlock(code)
  for _, u in ipairs(ctx.unlocks) do
    if u == code then return true end
  end
  return false
end

local function need_by_code(code)
  for _, n in ipairs(ctx.needs) do
    if n.code == code then return n end
  end
  return nil
end

-- Is a process of this recipe already RUNNING for this entity? Guards the
-- "start one per tick" idiom: a duration-1 process started last tick
-- completes at the top of this tick (before scripts), so by script time it
-- is no longer RUNNING and a fresh one may start -- steady state.
local function running_recipe(code)
  for _, p in ipairs(ctx.processes) do
    if p.recipe == code then return true end
  end
  return false
end

-- The first owned parcel carrying `facility_type`, or nil.
local function facility_parcel(facility_type)
  for _, p in ipairs(ctx.parcels) do
    for _, f in ipairs(p.facilities) do
      if f == facility_type then return p.id end
    end
  end
  return nil
end

-- The first owned parcel whose deposits include `symbol` (a mine seam), nil.
local function deposit_parcel(symbol)
  for _, p in ipairs(ctx.parcels) do
    if p.deposits[symbol] then return p.id end
  end
  return nil
end

-- Order feedback: cancel last tick's own orders, report fill ratios. -------
-- Orders are good-till-cancelled, so each script cancels the generation it
-- placed last tick and reads how it did. Returns fills["SYMBOL|side"] with
-- {ordered, filled, ratio}. Sell-side adaptation (below) consumes the ratio.
local function settle_last_orders()
  local by_order, fills = {}, {}

  for _, e in ipairs(ctx.events) do
    if e.type == "place_order" and e.status == "applied" and e.order_id then
      ctx.action.cancel_order(e.order_id, 1)
      by_order[e.order_id] = {
        key = e.params.symbol .. "|" .. e.params.side,
        ordered = tonumber(e.params.quantity) or 0,
        filled = 0,
      }
    end
  end
  for _, e in ipairs(ctx.events) do
    if e.order_id and by_order[e.order_id] and e.type == "trade" then
      by_order[e.order_id].filled = by_order[e.order_id].filled
        + (tonumber(e.quantity) or 0)
    end
  end
  for _, o in pairs(by_order) do
    local f = fills[o.key] or { ordered = 0, filled = 0 }
    f.ordered = f.ordered + o.ordered
    f.filled = f.filled + o.filled
    fills[o.key] = f
  end
  for key, f in pairs(fills) do
    if f.ordered > 0 then f.ratio = f.filled / f.ordered
    else fills[key] = nil end
  end
  return fills
end

-- Bounded sell-side adaptation: concede toward cost when stock went unsold,
-- firm up when it all moved. Clamped so it searches but never spirals. SELL
-- side only -- buyers quote true reservation prices and do not adapt (an
-- unfilled low-value buy is the auction working as intended).
local ADAPT_LO, ADAPT_HI = 0.3, 3.0
local function concede(fills, symbol)
  local factors = ctx.state.factors or {}
  local key = symbol .. "|sell"
  local factor = tonumber(factors[key]) or 1.0
  local f = fills[key]
  if f then
    local step = (f.ratio < 0.999) and -0.15 or 0.03
    factor = math.max(ADAPT_LO, math.min(ADAPT_HI, factor * (1 + step)))
  end
  factors[key] = factor
  ctx.state.factors = factors
  return factor
end

local function amount_str(x) return string.format("%.4f", x) end

-- Role actions -------------------------------------------------------------

-- Sell surplus of `symbol` (everything above `keep`) at `anchor * concede`.
local function sell_surplus(symbol, keep, anchor, account_id, fills)
  local qty = holding_qty(symbol) - keep
  if qty <= 0.01 then return end
  local ask = concede(fills, symbol) * anchor
  ctx.action.place_order(symbol, "sell", amount_str(qty),
                          amount_str(ask), account_id, 40)
end

-- Top up a GRAIN pantry of `pantry` units. The food BUYERS (miner, smith)
-- use this; a self-sufficient farmer never needs it (its own output fills
-- the pantry). Bids up to 3x the going price -- a hungry specialist will
-- pay, but the auction is uniform-price so the limit decides WHETHER, not
-- HOW MUCH. Returns the budget left after reserving the purchase.
local function buy_food(account_id, balance, grain_price, pantry)
  local want = pantry - holding_qty("GRAIN")
  if want <= 0.01 or balance <= 0 then return balance end
  local ref = (grain_price and grain_price > 0) and grain_price or 1.0
  local bid = math.min(ref * 3, balance / want)
  if bid <= 0 then return balance end
  local qty = math.min(want, balance / bid)
  if qty > 0.01 then
    ctx.action.place_order("GRAIN", "buy", amount_str(qty),
                            amount_str(bid), account_id, 30)
    balance = balance - qty * bid
  end
  return balance
end
