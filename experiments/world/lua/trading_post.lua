-- trading_post.lua  (BEHAVIOUR) -- the merchant of the stone age.
--
-- Run 49 postmortem: the counter was a FAUCET with a quote sheet. It
-- bought eggs (14 fills, ~60 coin) with no resale side, coin-broke at
-- t224, and 60% of the run traded against a corpse -- `selling JERKY
-- 1.00 | buying .`, 0.98 coin, a BED and 0.8 WARMTH. The wood-bid
-- caravan fuel never arrived (Lagertha sat on 70 logs all run), so the
-- famine rung never fired once.
--
-- Run 50's post is a MERCHANT (three trades at once):
--   a) PROFIT-SEEKING RESALE: nothing is bought without a resale
--      plan. Every lot carries a cost basis (fills blend into it),
--      retail = max(basis x 1.30, wholesale x 1.05) -- stock that
--      isn't there isn't quoted, and stock that IS there is priced
--      off what it cost. The egg pump is dead: the post pays at most
--      0.70 x wholesale for anything, because that is what the hills
--      charge IT.
--   b) IT PAYS TO EXIST: one log and one meal a day keep the counter
--      open (KEEP_SHOP_* refills SHOP_OPEN, which fades in a day).
--      No wood and no meal and the counter goes DARK -- no quotes, no
--      peddle, one honest say -- a visibly failing shop instead of a
--      zombie corpse quoting prices it cannot honour.
--   c) THE WHOLESALE BACK CHANNEL: the hills are an outside economy
--      that deals ONLY with the shop key. WHOLESALE_BUY_* pays coin
--      (debited at start -- the hills do not extend credit) for a
--      caravan-lot of stock when a staple runs under its minimum;
--      WHOLESALE_DUMP_* converts overstock lots back to coin at the
--      dump price (always under wholesale: the spread is the toll).
--      The wholesale anchors ARE the price band the seats face --
--      the real lever for prices, and the dial to turn OFF (delete
--      the recipes) once houses trade houses.
--
-- It still haggles like a person, INSIDE the band:
--   sold food            -> ask adjust x1.05   (demand is real)
--   3 live ticks quiet   -> ask adjust x0.95 / bid x1.03 (move the
--                          price toward trade instead of waiting)
--   bought goods         -> bid x0.95          (sellers are eager)
-- Famine rungs: every wholesale jerky lot retails one rung up
-- (+0.75 -- dear calories in a hungry world); real supply (a MEAT
-- fill, a salt batch) walks the ladder back down.
--
-- Vocabulary: std.* (engine), ctx.action.place_order / cancel_order /
-- start_process / say, ctx.events (the post's own fills, applied
-- orders and back-room batches from last tick), ctx.processes (its
-- running errands), ctx.accounts (the purse).

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

-- The wholesale ladder (mirrors WHOLESALE in stone_age.py -- the
-- consistency test in test_stone_age pins the two together). w = the
-- hills' charge per unit; d = the dump price they pay for overstock;
-- lot = the caravan-sized batch the recipes move.
local WHOLESALE = {
  JERKY     = { w = 0.90, d = 0.45, lot = 6,  buy = true,  dump = false },
  BERRIES   = { w = 0.50, d = 0.25, lot = 10, buy = true,  dump = true },
  APPLES    = { w = 0.70, d = 0.35, lot = 10, buy = true,  dump = true },
  EGGS      = { w = 0.70, d = 0.35, lot = 8,  buy = true,  dump = true },
  CHICKEN   = { w = 3.00, d = 1.50, lot = 2,  buy = true,  dump = false },
  WATERSKIN = { w = 4.50, d = 2.25, lot = 2,  buy = true,  dump = false },
  WOOD      = { w = 1.00, d = 0.50, lot = 10, buy = true,  dump = true },
  MEAT      = { w = 1.00, d = 0.50, lot = 4,  buy = false, dump = true },
  YARN      = { w = 1.50, d = 0.75, lot = 10, buy = false, dump = true },
  FLINT     = { w = 1.50, d = 0.75, lot = 10, buy = false, dump = true },
  PELT      = { w = 3.00, d = 1.50, lot = 4,  buy = false, dump = true },
}
local STOCK = {          -- bands: under min the wholesale door opens,
  JERKY = {min = 6, max = 12},  -- over max the bids stop and the dump
  BERRIES = {min = 10, max = 20}, -- door opens. A larder, not a landfill.
  APPLES = {min = 10, max = 20},
  EGGS = {min = 8, max = 16},
  CHICKEN = {min = 2, max = 4},
  WATERSKIN = {min = 2, max = 4},
  WOOD = {min = 6, max = 30},
  MEAT = {min = 0, max = 20},
  YARN = {min = 0, max = 12},
  FLINT = {min = 0, max = 12},
  PELT = {min = 0, max = 8},
}
local RETAIL = { "JERKY", "BERRIES", "APPLES", "EGGS", "CHICKEN",
                 "WATERSKIN" }
local FOREST = { "MEAT", "WOOD", "YARN", "FLINT", "BERRIES",
                 "APPLES", "EGGS", "PELT" }
-- Genesis one-offs with no wholesale anchor (they rot, or they are
-- gone when gone): haggled directly, like the old book.
local LEGACY_ASK = { COOKED_MEAT = 1.50, BED = 5.00 }

local MARGIN = 1.30      -- retail = cost basis x margin
local BID_CAP = 0.70     -- ... never bid above this fraction of w
local ASK_FLOOR = 1.05   -- ... never retail below this fraction of w
local BID_FLOORF = 0.25  -- ... nor bid below a quarter of w
local ASK_CAP = 8.00
local RESERVE = 6.00     -- coin the wholesale door never dips into

local function r2(x) return string.format("%.2f", x) end

if not S.bid then
  -- Opening bids start at half the hills' charge; they may climb to
  -- 0.70 x w -- that is the most stock is worth to a shop that can
  -- always send a runner instead. Run 49's 5.00 egg bid is gone: the
  -- hills underbid every seat at the wholesale counter, and the post
  -- knows it.
  S.bid, S.cost, S.adj = {}, {}, {}
  for _, sym in ipairs(FOREST) do
    S.bid[sym] = r2(WHOLESALE[sym].w * 0.5)
  end
  S.ask = { COOKED_MEAT = 1.50, BED = 5.00 }
  S.rungs = 0            -- the famine ladder (wholesale jerky retails dear)
  S.quiet = {}   -- LIVE ticks since the last fill, per "side_SYMBOL" key
  S.live = {}    -- the (qty, price) placed per key, to spot drift
  S.ids = {}     -- order id per key, from applied place_order events
  S.answered = {} -- last tick each house was answered at the counter
end

-- 1. Age only the orders that are actually resting. A dark key (no
--    budget, no larder, dark shop) keeps its price frozen, ready to
--    return.
for key in pairs(S.live) do
  S.quiet[key] = (S.quiet[key] or 0) + 1
end

-- 2. Yesterday's news: fills move prices and bases; applied orders
--    bring ids; the back room reports its batches.
local function blend(sym, price, qty)
  -- exact weighted average into the cost basis: what the counter
  -- actually paid is what its retail must clear (the run-49 egg pump
  -- died precisely because nothing kept the books honest).
  local now = std.holding_qty(sym)
  local prev = math.max(now - qty, 0)
  local old = S.cost[sym] or WHOLESALE[sym].w
  if prev + qty > 0 then
    S.cost[sym] = (old * prev + price * qty) / (prev + qty)
  end
end

for _, e in ipairs(ctx.events or {}) do
  if e.type == "trade" then
    local key = e.side .. "_" .. e.market
    S.quiet[key] = 0
    if e.side == "sell" then       -- we sold: demand, ask up
      if LEGACY_ASK[e.market] then
        S.ask[e.market] = math.min(S.ask[e.market] * 1.05, ASK_CAP)
      else
        S.adj[e.market] = math.min((S.adj[e.market] or 1) * 1.05, 1.5)
      end
    else                           -- we bought: supply, bid down, basis in
      local q = tonumber(e.quantity) or 1
      local p = tonumber(e.price) or WHOLESALE[e.market].w
      if S.bid[e.market] then
        S.bid[e.market] = math.max(S.bid[e.market] * 0.95,
                                   WHOLESALE[e.market].w * BID_FLOORF)
      end
      blend(e.market, p, q)
      if e.market == "MEAT" then
        -- the salt anchor: two raw make two jerky, so a meat fill IS a
        -- jerky basis (the 1:1 transform) -- and real supply walks the
        -- famine ladder back down.
        S.cost.JERKY = p
        S.rungs = math.max(0, (S.rungs or 0) - 1)
      end
    end
  elseif e.type == "process_completed" and e.entity_id == ctx.entity.id then
    if e.recipe == "SALT_MEAT" then
      -- a batch came off the rack: fresh stock is fresh news, and the
      -- ladder eases (the forest fed him, no need for dear calories)
      S.rungs = math.max(0, (S.rungs or 0) - 1)
      S.quiet["sell_JERKY"] = 0
    elseif e.recipe == "WHOLESALE_BUY_JERKY" then
      -- the famine rung: this lot retails dear -- wholesale calories
      -- are what a BARE shelf demanded, and the demand was proven.
      S.rungs = (S.rungs or 0) + 1
      S.quiet["sell_JERKY"] = 0
    end
  elseif e.type == "place_order" and e.status == "applied"
         and e.order_id then
    S.ids[e.params.side .. "_" .. e.params.symbol] = e.order_id
  end
end

-- 2a. Upkeep (run 49's honest merchant): SHOP_OPEN fades in a day;
--     under a third left, the keeper re-ups -- one log, one meal off
--     his own shelf. No wood or no meal and the counter goes DARK.
local function keep_shop()
  if std.holding_qty("SHOP_OPEN") >= 0.30 then return end
  if std.holding_qty("WOOD") < 1 then return end
  for _, sym in ipairs({ "JERKY", "EGGS", "APPLES", "BERRIES" }) do
    if std.holding_qty(sym) >= 1 then
      ctx.action.start_process("KEEP_SHOP_" .. sym)
      return
    end
  end
end
keep_shop()

local dark = std.holding_qty("SHOP_OPEN") < 0.05
if dark then
  if not S.dark_said then
    ctx.action.say("POST: the counter is dark -- no wood for the crates "
      .. "and no meal for the keeper. Bring logs or food and it opens "
      .. "again.")
    S.dark_said = true
  end
else
  S.dark_said = false
end

-- 2b. The salt shed (run 46): bought MEAT is rotting inventory --
--     0.30/tick, the post's own MEAT bid literally evaporates in a
--     larder -- so it goes to the back room promptly: while raw meat
--     covers a batch (2) and a rack stands free (capacity 2), salt.
--     No labor, no fire, no daylight: the shed works the night shift.
--     Salt comes FIRST: what the forest sells is the cheaper rung --
--     wholesale only what the forest will not (run 48's lesson).
local racks = 0        -- SALT_MEAT and every wholesale runner share
local running = {}     -- the shed's two racks
for _, p in ipairs(ctx.processes or {}) do
  running[p.recipe] = true
  if p.recipe == "SALT_MEAT" or p.recipe:sub(1, 10) == "WHOLESALE_" then
    racks = racks + 1
  end
end

while std.holding_qty("MEAT") >= 2 and racks < 2 do
  ctx.action.start_process("SALT_MEAT")
  racks = racks + 1
end

-- 2c. The wholesale doors (run 50): under minimum stock, coin beyond
--     the RESERVE buys a caravan lot (cheapest errand first); over the
--     band's max, a lot goes back over the hills for coin at the dump
--     price (most overweight first). THE KEY IS THE RUNNER: a
--     good-requirement is reserved while its process runs, so one
--     SHOP_KEY means one errand at a time -- restock beats disposal,
--     and the hills are a counterparty, not a firehose.
if not dark then
  local acct, coin = nil, 0
  for _, a in ipairs(ctx.accounts) do
    if a.currency == "COIN" then acct, coin = a.id, tonumber(a.balance) end
  end

  local errand_out = false
  for code in pairs(running) do
    if code:sub(1, 10) == "WHOLESALE_" then errand_out = true end
  end

  if not errand_out and racks < 2 then
    local buys, dumps = {}, {}
    for sym, spec in pairs(WHOLESALE) do
      local band = STOCK[sym] or { min = spec.lot, max = spec.lot * 2 }
      local held = std.holding_qty(sym)
      if spec.buy and held < band.min
         and coin >= spec.w * spec.lot + RESERVE then
        buys[#buys + 1] = { code = "WHOLESALE_BUY_" .. sym,
                            cost = spec.w * spec.lot }
      end
      if spec.dump and held > band.max and held >= spec.lot then
        dumps[#dumps + 1] = { code = "WHOLESALE_DUMP_" .. sym,
                              over = held - band.max }
      end
    end
    table.sort(buys, function(a, b) return a.cost < b.cost end)
    table.sort(dumps, function(a, b) return a.over > b.over end)
    if #buys > 0 then
      ctx.action.start_process(buys[1].code)
    elseif #dumps > 0 then
      ctx.action.start_process(dumps[1].code)
    end
  end
end

-- 3. Quiet drift: 3 live ticks without a fill eases the price toward
--    trade -- bids up toward the wholesale cap, ask adjust down.
for _, sym in ipairs(RETAIL) do
  if (S.quiet["sell_" .. sym] or 0) >= 3 then
    S.adj[sym] = math.max((S.adj[sym] or 1) * 0.95, 0.9)
    S.quiet["sell_" .. sym] = 0
  end
end
for _, sym in ipairs(FOREST) do
  if (S.quiet["buy_" .. sym] or 0) >= 3 then
    S.bid[sym] = math.min(S.bid[sym] * 1.03,
                          WHOLESALE[sym].w * BID_CAP)
    S.quiet["buy_" .. sym] = 0
  end
end
for sym in pairs(LEGACY_ASK) do
  if (S.quiet["sell_" .. sym] or 0) >= 3 then
    S.ask[sym] = math.max(S.ask[sym] * 0.95, 0.50)
    S.quiet["sell_" .. sym] = 0
  end
end

-- 4. What should stand now? The wholesale band prices the book:
--    retail = max(basis x MARGIN, w x ASK_FLOOR) x adjust + famine
--    rungs; stock that isn't held isn't quoted. Bids stand only under
--    the band max, and never commit the RESERVE.
local acct, coin = nil, 0
for _, a in ipairs(ctx.accounts) do
  if a.currency == "COIN" then acct, coin = a.id, tonumber(a.balance) end
end

local function ask_of(sym)
  local spec = WHOLESALE[sym]
  local a = math.max((S.cost[sym] or spec.w) * MARGIN,
                     spec.w * ASK_FLOOR)
    * (S.adj[sym] or 1)
  if sym == "JERKY" then a = a + 0.75 * (S.rungs or 0) end
  return r2(math.min(a, ASK_CAP))
end

local want = {}
if not dark then
  for _, sym in ipairs(RETAIL) do
    local qty = math.floor(std.holding_qty(sym))
    if qty > 0 then
      want["sell_" .. sym] = { qty = qty, price = ask_of(sym),
                               symbol = sym, side = "sell" }
    end
  end
  for sym in pairs(LEGACY_ASK) do
    local qty = math.floor(std.holding_qty(sym))
    if qty > 0 then
      want["sell_" .. sym] = { qty = qty, price = r2(S.ask[sym]),
                               symbol = sym, side = "sell" }
    end
  end
end

-- The purse is split pro-rata over the bids the bands still allow,
-- and never dips into the RESERVE (the wholesale door eats first).
local spendable = math.max(coin - RESERVE, 0)
local desired, total_cost = {}, 0
for _, sym in ipairs(FOREST) do
  local band = STOCK[sym] or { max = WHOLESALE[sym].lot * 2 }
  local price = S.bid[sym]
  if not dark and price and std.holding_qty(sym) < band.max then
    desired[#desired + 1] = { key = "buy_" .. sym, symbol = sym,
                              price = price, qty = 4 }
    total_cost = total_cost + 4 * price
  end
end
local scale = 1
if total_cost > spendable and total_cost > 0 then
  scale = spendable / total_cost
end
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
    if spent + d.price <= spendable + 0.000001 then
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

-- Anything that stopped being wanted (larder empty, budget gone, band
-- full, dark shop) must leave the book AND the aging rolls: freeze,
-- don't drift in the dark.
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
--    in the book. Cancelling an filled order is an idempotent no-op.
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
--    anyone going looking. The menu is what actually rests -- asks
--    only for larder on hand, bids only where coin stands behind them
--    -- and twice a round (every 10th tick) keeps it ambient without
--    drowning rival speech in the 50-tick digest houses read.
--    AFTER DARK THE COUNTER GOES QUIET: at night, speech is a beacon
--    (wolves hunt by ear). He is old, careful, and tooled up -- he
--    does not draw maps to his firelight.
local function menu()
  local sells, buys = {}, {}
  for _, sym in ipairs(RETAIL) do
    if want["sell_" .. sym] and #sells < 6 then
      sells[#sells + 1] = sym .. " " .. (want["sell_" .. sym].price)
    end
  end
  for sym in pairs(LEGACY_ASK) do
    if want["sell_" .. sym] and #sells < 6 then
      sells[#sells + 1] = sym .. " " .. (want["sell_" .. sym].price)
    end
  end
  for _, sym in ipairs(FOREST) do
    if want["buy_" .. sym] and #buys < 6 then
      buys[#buys + 1] = sym .. " " .. r2(S.bid[sym])
    end
  end
  return sells, buys
end

if ctx.tick % 10 == 0 and not dark and not std.is_night() then
  local sells, buys = menu()
  if #sells + #buys > 0 then
    ctx.action.say("POST: selling " .. table.concat(sells, ", ")
      .. " | buying " .. table.concat(buys, ", ")
      .. ". Sell me your surplus for coin.")
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
     and not dark and not std.is_night()
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
