-- Content-pack helpers (docs/scripting.md section 2): the play OPINIONS
-- this pack ships -- bounded sell-side price adaptation and the pantry/bid
-- food policy. Concatenated ahead of each role script by scenario
-- _behaviour: the sandbox has no `require`, and the engine `std` / `world`
-- namespaces arrive injected, so this is the only library text a script
-- must carry in its own source. That a player's get_behaviour shows these
-- helpers is the point -- they are the strategy the starter inherits, to
-- keep, drop, or rewrite.

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

-- Sell surplus of `symbol` (everything above `keep`) at `anchor * concede`.
local function sell_surplus(symbol, keep, anchor, account_id, fills)
  local qty = std.holding_qty(symbol) - keep
  if qty <= 0.01 then return end
  local ask = concede(fills, symbol) * anchor
  ctx.action.place_order(symbol, "sell", std.amount_str(qty),
                          std.amount_str(ask), account_id, 40)
end

-- Top up a GRAIN pantry of `pantry` units. The food BUYERS (miner, smith)
-- use this; a self-sufficient farmer never needs it (its own output fills
-- the pantry). Bids up to 3x the going price -- a hungry specialist will
-- pay, but the auction is uniform-price so the limit decides WHETHER, not
-- HOW MUCH. Returns the budget left after reserving the purchase.
local function buy_food(account_id, balance, grain_price, pantry)
  local want = pantry - std.holding_qty("GRAIN")
  if want <= 0.01 or balance <= 0 then return balance end
  local ref = (grain_price and grain_price > 0) and grain_price or 1.0
  local bid = math.min(ref * 3, balance / want)
  if bid <= 0 then return balance end
  local qty = math.min(want, balance / bid)
  if qty > 0.01 then
    ctx.action.place_order("GRAIN", "buy", std.amount_str(qty),
                            std.amount_str(bid), account_id, 30)
    balance = balance - qty * bid
  end
  return balance
end
