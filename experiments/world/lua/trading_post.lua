-- trading_post.lua  (BEHAVIOUR) -- the market maker of the stone age.
--
-- The post SELLS safe food (BERRIES, COOKED_MEAT, JERKY), the
-- larder's seed capital (CHICKEN -- P5), and the water kit's anchor
-- (WATERSKIN -- P3: one skin, priced at a pelt and a half of coin --
-- the price a thirsty house reads), and BUYS raw goods (MEAT,
-- WOOD, YARN, FLINT, BERRIES, APPLES, EGGS, PELT) for COIN. Three runs
-- showed why it must exist: houses with coin and hunger found no
-- seller, hunters with meat found no buyer, and "the price is
-- unknown" froze the one agent that tried to trade. A standing
-- counterparty IS the fix: every order the post quotes is a price
-- reference the whole world can read. JERKY is salted meat: it never
-- rots, so the shop always has SOMETHING to sell, however late the
-- customer arrives (run 4: OSS died with 17 COIN in a world whose
-- larder had rotted). The hens are the pastoral ladder's seed: the
-- post holds two, sells them at 4.00, and stands at 1.20 an egg --
-- a hen pays her price back in four eggs (P5).
--
-- THE SHELF RESTOCKS ITSELF (run 46, lever 1): the post was a
-- finite faucet -- it sold its last jerky early and never bought a
-- meal back, so the world's only coin source went broke AND empty
-- and the market died with it. Now the back room salts what the
-- forest sells him: every 2 MEAT become 2 JERKY at his own shed
-- (an OWNER facility no house can bind), and the jerky ask is
-- anchored off the fills that stocked him -- twice the raw cost
-- (the opening book quoted MEAT 1.00 / JERKY 2.00, the same 2:1:
-- preservation is what a house pays for). The counter is a
-- transform now, not a faucet.
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
-- ctx.events (the post's own fills, applied orders and salt batches
-- from last tick).

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
  -- Opening asks price a FULL DAY of food under the seat purse (10
  -- COIN, ~14 satiety/day): berries 7 meals × 1.25 = 8.75, cooked
  -- 6 × 1.50 = 9.00, jerky 4 × 2.00 = 8.00. Runs 26-28 died with the
  -- whole purse unspent -- coin nobody could eat. BERRIES stays above
  -- its 1.00 bid so the post never crosses itself. CHICKEN is priced
  -- against its own wage (P5): a hen lays ~1 egg/day the post buys at
  -- 1.20, so 4.00 asks four days of laying -- capital that returns
  -- itself, quoted so a house can see the arithmetic before it pays.
  -- WATERSKIN (P3): one PELT makes one by hand, the post's PELT bid
  -- is 3.00 and the egg trade pays 1.20 -- 6.00 stands between "sew
  -- it yourself" and "coin is quicker", the anchor thirsty houses
  -- read. One on the shelf; when it is gone, it is gone.
  -- BED (P4): 1 LABOR + 2 WOOD + 3 YARN by hand (the egg trade pays
  -- the yarn) -- 5.00 stands between "build it yourself" and "coin
  -- is quicker", the comfort ladder's storefront. One on the shelf;
  -- when it is gone, it is gone.
  S.ask = { BERRIES = 1.25, COOKED_MEAT = 1.50, JERKY = 2.00,
            CHICKEN = 4.00, WATERSKIN = 6.00, BED = 5.00 }
  S.bid = { BERRIES = 1.00, MEAT = 1.00, WOOD = 1.00,
            YARN = 2.00, FLINT = 2.00, PELT = 3.00,
            APPLES = 0.80, EGGS = 1.20 }
  S.quiet = {}   -- LIVE ticks since the last fill, per "side_SYMBOL" key
  S.live = {}    -- the (qty, price) placed per key, to spot drift
  S.ids = {}     -- order id per key, from applied place_order events
  S.answered = {} -- last tick each house was answered at the counter
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
      -- The shed's cost ledger: the price the counter actually PAID
      -- for raw meat (the clearing price of the fill, at or under our
      -- bid) is the jerky shelf's cost basis -- the anchor the ask
      -- follows (below).
      if e.market == "MEAT" and e.price then
        S.cost_meat = tonumber(e.price)
        S.ask.JERKY = r2(math.min(math.max((S.cost_meat or 1.00) * 2,
                                           1.50), ASK_CAP))
        S.quiet["sell_JERKY"] = 0  -- restocked at a known cost: fresh
      end
    end
  elseif e.type == "process_completed" and e.recipe == "SALT_MEAT"
         and e.entity_id == ctx.entity.id then
    -- A batch came off the rack: re-anchor the jerky ask off the
    -- fills that stocked it (haggling may have walked it far from
    -- cost while the meat was raw) and restart the quiet clock --
    -- fresh stock is fresh news.
    S.ask.JERKY = r2(math.min(math.max((S.cost_meat or 1.00) * 2,
                                       1.50), ASK_CAP))
    S.quiet["sell_JERKY"] = 0
  elseif e.type == "place_order" and e.status == "applied"
         and e.order_id then
    S.ids[e.params.side .. "_" .. e.params.symbol] = e.order_id
  end
end

-- 2b. The salt shed (run 46): bought MEAT is rotting inventory --
--     0.30/tick, the post's own MEAT bid literally evaporates in a
--     larder -- so it goes to the back room promptly: while raw meat
--     covers a batch (2) and a rack stands free (capacity 2), salt.
--     No labor, no fire, no daylight: the shed works the night shift.
local salting = 0
for _, p in ipairs(ctx.processes or {}) do
  if p.recipe == "SALT_MEAT" then salting = salting + 1 end
end
while std.holding_qty("MEAT") >= 2 and salting < 2 do
  ctx.action.start_process("SALT_MEAT")
  salting = salting + 1
end

-- 3. Quiet drift: 3 live ticks without a fill eases the price toward
--    trade.
for _, sym in ipairs({ "BERRIES", "COOKED_MEAT", "JERKY", "CHICKEN",
                       "WATERSKIN", "BED" }) do
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
for _, sym in ipairs({ "BERRIES", "COOKED_MEAT", "JERKY", "CHICKEN",
                       "WATERSKIN", "BED" }) do
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
for _, sym in ipairs({ "MEAT", "WOOD", "YARN", "FLINT", "BERRIES",
                       "APPLES", "EGGS", "PELT" }) do
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
local function menu()
  local sells, buys = {}, {}
  for _, sym in ipairs({ "BERRIES", "COOKED_MEAT", "JERKY", "CHICKEN",
                         "WATERSKIN" }) do
    if want["sell_" .. sym] then
      sells[#sells + 1] = sym .. " " .. r2(S.ask[sym])
    end
  end
  for _, sym in ipairs({ "MEAT", "WOOD", "YARN", "FLINT", "BERRIES",
                         "APPLES", "EGGS", "PELT" }) do
    if want["buy_" .. sym] then
      buys[#buys + 1] = sym .. " " .. r2(S.bid[sym])
    end
  end
  return sells, buys
end

if ctx.tick % 10 == 0 and not std.is_night() then
  local sells, buys = menu()
  if #sells + #buys > 0 then
    ctx.action.say("POST: selling " .. table.concat(sells, ", ")
      .. " | buying " .. table.concat(buys, ", ")
      .. ". Sell me your surplus for coin -- the shelf feeds the lean days.")
  end
end

-- 6b. The counter ANSWERS (run 45: a house starved broke at a full
--     larder, holding ten wood against a shouted bid of five -- the
--     book was legible, nobody crossed the aisle). The merchant does
--     not cold-call and he does not read purses: he answers the
--     customer's own move, and only that. A buy that bounced at his
--     counter -- the empty purse he watched try to pay, now a loud
--     fact -- gets the sell-side quote in plain speech; a house that
--     SPEAKS at the counter gets the book. One answer per house per
--     six hours, day only: after dark the counter stays quiet
--     (speech is a beacon, and the wolves hunt by ear).
for _, e in ipairs(ctx.events or {}) do
  if e.entity_id and e.entity_id ~= ctx.entity.id
     and (ctx.tick - (S.answered[e.entity_id] or -999)) >= 6
     and not std.is_night()
     and ((e.type == "order_cancelled"
           and e.reason == "insufficient funds at auction")
          or (e.type == "say" and e.status ~= "rejected")) then
    local sells, buys = menu()
    if e.type == "order_cancelled" and #buys > 0 then
      S.answered[e.entity_id] = ctx.tick
      ctx.action.say("POST: no coin at my counter? I pay coin for "
        .. table.concat(buys, ", ")
        .. " -- sell me what you carry and eat tonight.")
    elseif #sells + #buys > 0 then
      S.answered[e.entity_id] = ctx.tick
      ctx.action.say("POST: heard at the counter -- selling "
        .. table.concat(sells, ", ") .. " | buying "
        .. table.concat(buys, ", ")
        .. ". The book stands; the shelf feeds the lean days.")
    end
  end
end
