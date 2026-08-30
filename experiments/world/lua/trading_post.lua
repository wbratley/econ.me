-- trading_post.lua  (BEHAVIOUR) -- the market maker of the stone age.
--
-- The post SELLS safe food (BERRIES, COOKED_MEAT, JERKY) and BUYS raw
-- goods (MEAT, WOOD, YARN, FLINT, BERRIES) for COIN. Three runs showed
-- why it must exist: houses with coin and hunger found no seller,
-- hunters with meat found no buyer, and "the price is unknown" froze
-- the one agent that tried to trade. A standing counterparty IS the
-- fix: every order the post quotes is a price reference the whole
-- world can read. JERKY is salted meat: it never rots, so the shop
-- always has SOMETHING to sell, however late the customer arrives
-- (run 4: OSS died with 17 COIN in a world whose larder had rotted).
--
-- It haggles like a person would:
--   sold food            -> ask +5%          (demand is real, charge it)
--   bought goods         -> bid -5%          (sellers are eager, pay less)
--   3 ticks LIVE, no fills -> ask -5% / bid +3% (move the price toward
--                          the market instead of waiting). Quiet is
--                          counted only while an order actually rests:
--                          a bid gone dark (no budget for it) freezes
--                          its price instead of walking it to the cap
--                          (run 4: dark bids drifted to 5.00 for nothing).
-- The purse is split pro-rata across every good it wants: a lean
-- budget shrinks all bids together instead of letting the head of the
-- line (MEAT, WOOD) eat the coin and starve the tail (run 4 again).
-- Bids never commit more COIN than the post holds, and it stops
-- bidding for any good it already holds 20 of -- a larder, not a
-- landfill. Prices and standing order ids live in ctx.state, so the
-- haggling persists across ticks.
--
-- Vocabulary: std.* (engine), ctx.action.place_order / cancel_order,
-- ctx.events (the post's own fills and applied orders from last tick).

local S = ctx.state

-- 0. The trader is a man, not a building: something came at him in
--    the night, he answers -- armed and by the door he keeps. The
--    world keeps his hearth lit; the fire turns wolves; his hands
--    close the account.
for _, e in ipairs(ctx.events or {}) do
  if e.type == "combat" and e.target_id == ctx.entity.id
     and e.entity_id and e.entity_id ~= ctx.entity.id
     and (e.hit or e.deterred) then
    ctx.action.attack(e.entity_id)
  end
end

if not S.ask then
  S.ask = { BERRIES = 2.00, COOKED_MEAT = 3.00, JERKY = 3.00 }
  S.bid = { BERRIES = 1.00, MEAT = 1.00, WOOD = 1.00,
            YARN = 2.00, FLINT = 2.00, PELT = 3.00 }
  S.quiet = {}   -- LIVE ticks since the last fill, per "side_SYMBOL" key
  S.live = {}    -- the (qty, price) placed per key, to spot drift
  S.ids = {}     -- order id per key, from applied place_order events
end

local ASK_FLOOR, ASK_CAP = 1.00, 8.00
local BID_FLOOR, BID_CAP = 0.50, 5.00
local APPETITE = 20        -- stop bidding for a good held past this

local function r2(x) return string.format("%.2f", x) end

-- 1. Age only the orders that are actually resting. A dark key (no
--    budget, no larder) keeps its price frozen, ready to return.
for key in pairs(S.live) do
  S.quiet[key] = (S.quiet[key] or 0) + 1
end

-- 2. Yesterday's news: fills move prices; applied orders bring ids.
for _, e in ipairs(ctx.events or {}) do
  if e.type == "trade" then
    local key = e.side .. "_" .. e.market
    S.quiet[key] = 0
    if e.side == "sell" then       -- we sold food: demand, ask up
      S.ask[e.market] = math.min(S.ask[e.market] * 1.05, ASK_CAP)
    else                           -- we bought goods: supply, bid down
      S.bid[e.market] = math.max(S.bid[e.market] * 0.95, BID_FLOOR)
    end
  elseif e.type == "place_order" and e.status == "applied"
         and e.order_id then
    S.ids[e.params.side .. "_" .. e.params.symbol] = e.order_id
  end
end

-- 3. Quiet drift: 3 live ticks without a fill eases the price toward
--    trade.
for _, sym in ipairs({ "BERRIES", "COOKED_MEAT", "JERKY" }) do
  if (S.quiet["sell_" .. sym] or 0) >= 3 then
    S.ask[sym] = math.max(S.ask[sym] * 0.95, ASK_FLOOR)
    S.quiet["sell_" .. sym] = 0
  end
end
for sym in pairs(S.bid) do
  if (S.quiet["buy_" .. sym] or 0) >= 3 then
    S.bid[sym] = math.min(S.bid[sym] * 1.03, BID_CAP)
    S.quiet["buy_" .. sym] = 0
  end
end

-- 4. What should stand now? Sell the whole larder at the ask; bid for
--    a little of everything the forest offers, within the coin on hand.
local acct, coin = nil, 0
for _, a in ipairs(ctx.accounts) do
  if a.currency == "COIN" then acct, coin = a.id, tonumber(a.balance) end
end

local want = {}
for _, sym in ipairs({ "BERRIES", "COOKED_MEAT", "JERKY" }) do
  local qty = math.floor(std.holding_qty(sym))
  if qty > 0 then
    want["sell_" .. sym] = { qty = qty, price = r2(S.ask[sym]),
                             symbol = sym, side = "sell" }
  end
end

-- The purse is split pro-rata: every affordable bid shares the lean
-- years. Desired qty 4 each; if the total exceeds the coin on hand,
-- ALL quantities shrink by the same ratio -- no head-of-line feeding.
local desired, total_cost = {}, 0
for _, sym in ipairs({ "MEAT", "WOOD", "YARN", "FLINT", "BERRIES" }) do
  local price = S.bid[sym]
  -- never cross our own ask (the post will not trade with itself)
  if price and (not S.ask[sym] or price < S.ask[sym])
     and std.holding_qty(sym) < APPETITE then
    desired[#desired + 1] = { key = "buy_" .. sym, symbol = sym,
                              price = price, qty = 4 }
    total_cost = total_cost + 4 * price
  end
end
local scale = 1
if total_cost > coin and total_cost > 0 then scale = coin / total_cost end
local qtys, spread = {}, false
for _, d in ipairs(desired) do
  local q = math.floor(d.qty * scale + 0.000001)
  qtys[d.key] = q
  if q > 0 then spread = true end
end
if not spread and #desired > 0 then
  -- Purse too thin to spread even one unit each: cheapest goods
  -- first, one unit at a time, within the coin on hand.
  table.sort(desired, function(a, b) return a.price < b.price end)
  local spent = 0
  for _, d in ipairs(desired) do
    if spent + d.price <= coin + 0.000001 then
      qtys[d.key] = 1
      spent = spent + d.price
    end
  end
end
for _, d in ipairs(desired) do
  local qty = qtys[d.key]
  if qty > 0 then
    want[d.key] = { qty = qty, price = r2(d.price), symbol = d.symbol,
                    side = "buy" }
  end
end

-- Anything that stopped being wanted (larder empty, budget gone, hit
-- appetite) must leave the book AND the aging rolls: freeze, don't
-- drift in the dark.
for key in pairs(S.live) do
  if not want[key] then
    local id = S.ids[key]
    if id then ctx.action.cancel_order(id) end
    S.live[key] = nil
    S.quiet[key] = nil
    S.ids[key] = nil
  end
end

-- 5. Reconcile: cancel + replace only where (qty, price) moved; new
--    wants place directly. Untouched orders keep their time priority
--    in the book. Cancelling an order that already filled is an
--    idempotent no-op -- the engine just says yes to "rest nothing".
for key, w in pairs(want) do
  local cur = S.live[key]
  if not (cur and cur.qty == w.qty and cur.price == w.price) then
    local id = S.ids[key]
    if id then ctx.action.cancel_order(id) end
    ctx.action.place_order(w.symbol, w.side, w.qty, w.price, acct)
    S.live[key] = w
    S.ids[key] = nil      -- the new id arrives in next tick's events
  end
end

-- 6. Peddle the counter. The order book is passive -- it waits to be
--    read -- and five runs showed houses that never read it. Speech is
--    broadcast: every house's next prompt carries what it heard, so a
--    steady drumbeat here is the menu on the wall, remembered without
--    anyone going looking. Run 14: 9 WOOD sold to the post, zero food
--    bought FROM it; a shelf nobody recalls never moves. The menu is
--    what actually rests -- asks only for larder on hand, bids only
--    where coin stands behind them -- and twice a round (every 10th
--    tick) keeps it ambient without drowning rival speech in the
--    50-tick digest houses read.
--    AFTER DARK THE COUNTER GOES QUIET: at night, speech is a beacon
--    (wolves hunt by ear). He is old, careful, and tooled up -- he
--    does not draw maps to his firelight.
if ctx.tick % 10 == 0 and not std.is_night() then
  local sells, buys = {}, {}
  for _, sym in ipairs({ "BERRIES", "COOKED_MEAT", "JERKY" }) do
    if want["sell_" .. sym] then
      sells[#sells + 1] = sym .. " " .. r2(S.ask[sym])
    end
  end
  for _, sym in ipairs({ "MEAT", "WOOD", "YARN", "FLINT", "BERRIES" }) do
    if want["buy_" .. sym] then
      buys[#buys + 1] = sym .. " " .. r2(S.bid[sym])
    end
  end
  if #sells + #buys > 0 then
    ctx.action.say("POST: selling " .. table.concat(sells, ", ")
      .. " | buying " .. table.concat(buys, ", ")
      .. ". Sell me your surplus for coin; my shelf is food when "
      .. "your gathering fails.")
  end
end
