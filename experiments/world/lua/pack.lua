-- Content-pack lib (docs/scripting.md section 2, tier three): the play
-- OPINIONS this pack ships -- bounded sell-side price adaptation and the
-- pantry/bid food policy. A Lua chunk that RETURNS its namespace table,
-- injected as `pack` alongside `std` (engine) and `world` (this world's
-- idioms). Stored as the `scripting.pack_lib` WorldSetting, seeded at
-- bootstrap by scenario.create_content and pinned by pack.json.
--
-- Unlike the tiers beneath it, these helpers encode opinions about how to
-- play -- clamps, bid caps, priorities are strategy, not vocabulary. A
-- player who wants different strategy writes it in their own source; the
-- pack's opinions are the starting point they inherit, visible here and
-- via the get_script_libraries surface.

local pack = {}

-- Bounded sell-side adaptation: concede toward cost when stock went unsold,
-- firm up when it all moved. Clamped so it searches but never spirals. SELL
-- side only -- buyers quote true reservation prices and do not adapt (an
-- unfilled low-value buy is the auction working as intended).
local ADAPT_LO, ADAPT_HI = 0.3, 3.0
function pack.concede(fills, symbol)
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
function pack.sell_surplus(symbol, keep, anchor, account_id, fills)
  local qty = std.holding_qty(symbol) - keep
  if qty <= 0.01 then return end
  local ask = pack.concede(fills, symbol) * anchor
  ctx.action.place_order(symbol, "sell", std.amount_str(qty),
                          std.amount_str(ask), account_id)
end

-- Top up a GRAIN pantry of `pantry` units. The food BUYERS (miner, smith)
-- use this; a self-sufficient farmer never needs it (its own output fills
-- the pantry). Bids up to 3x the going price -- a hungry specialist will
-- pay, but the auction is uniform-price so the limit decides WHETHER, not
-- HOW MUCH. Returns the budget left after reserving the purchase.
function pack.buy_food(account_id, balance, grain_price, pantry)
  local want = pantry - std.holding_qty("GRAIN")
  if want <= 0.01 or balance <= 0 then return balance end
  local ref = (grain_price and grain_price > 0) and grain_price or 1.0
  local bid = math.min(ref * 3, balance / want)
  if bid <= 0 then return balance end
  local qty = math.min(want, balance / bid)
  if qty > 0.01 then
    ctx.action.place_order("GRAIN", "buy", std.amount_str(qty),
                            std.amount_str(bid), account_id)
    balance = balance - qty * bid
  end
  return balance
end

return pack
