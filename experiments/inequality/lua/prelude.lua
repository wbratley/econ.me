-- Shared prelude, prepended verbatim to every BEHAVIOUR script by
-- scenario.py. Holds the parts that must behave identically for every agent:
-- holdings/price lookup, and the order-feedback machinery that replaced the
-- original "last market price +/- X%" pricing.
--
-- Why pricing works the way it does now
-- ------------------------------------
-- Every script used to quote `market_price(sym) * 1.1` to buy and
-- `market_price(sym) * 0.95` to sell. That is a pure positive feedback loop:
-- the auction's clearing price becomes the next tick's anchor, so whichever
-- side's multiplier happens to be more aggressive drags the price with it
-- indefinitely. Nothing in the loop refers to what the good is actually
-- worth, so there is no level for it to converge ON -- LABOR fell toward
-- zero while FOOD climbed past 10, both for the same reason.
--
-- The replacement has two parts:
--
-- 1. RESERVATION PRICES. Each agent quotes what the good is genuinely worth
--    TO IT, computed from fundamentals rather than from the last trade: a
--    firm values raw labor at the revenue the goods it makes will fetch
--    (marginal revenue product), a person values food against their hunger
--    and their means, a person's reservation wage rises with their savings.
--    The auction is uniform-price (markets.py::_clearing_price picks the
--    volume-maximizing price, breaking ties toward zero demand/supply
--    imbalance), so quoting a true reservation price never means overpaying
--    -- you pay the clearing price, not your limit. That makes honest
--    quoting safe, and it makes the clearing price mean something: it is
--    where the marginal buyer's valuation meets the marginal seller's.
--    Cross-market anchoring stays (a wage is priced off the value of the
--    food that labor grows) -- that is real economics, not the self-
--    reference that caused the ratchet.
--
-- 2. BOUNDED ADAPTATION, SELL SIDE ONLY. A seller's anchor is its cost,
--    which is an opening ask rather than a true reservation price -- a
--    firm holding food that is about to rot really will take less than it
--    paid. `adapt()` tracks a per-symbol multiplier that concedes when the
--    agent's own last order went unfilled and firms up when it filled
--    completely, CLAMPED to [ADAPT_LO, ADAPT_HI] so it can search but can
--    never compound its way anywhere the way the old formula could.
--
--    Buyers deliberately do not adapt. Their quotes already are true
--    reservation prices, and fill feedback would actively mislead them: at
--    any real equilibrium a buyer's low-value orders are SUPPOSED to go
--    unfilled (the firm's tool-making labor, the household's pantry
--    top-up), and reading that as "bid higher" walks straight back into
--    paying more for a thing than it is worth.
--
-- The nominal anchor
-- ------------------
-- Relative prices alone leave the price LEVEL undetermined: a wage priced
-- off food and food priced off wages is a loop with gain 1 that drifts
-- wherever it is pushed. What pins it here is that household demand is
-- quoted from cash and from FIXED REAL consumption rates (below) and never
-- from a market price -- so a given stock of money chasing a given real
-- flow of goods produces a determinate price level, and deflation stops
-- when buyers rather than sellers become the marginal side.

local ADAPT_LO, ADAPT_HI = 0.4, 2.5
local STEP_MISS = 0.10  -- unfilled: move toward the other side, fast
local STEP_HIT = 0.03   -- fully filled: probe back for a better deal, slowly

local EPSILON = 0.01    -- lowest quotable price; keeps orders well-formed

-- Every agent's own production function, mirroring the recipes and branch
-- tables in scenario.py::_create_recipes. Knowing what your own labor makes
-- is what lets a firm price labor at its marginal revenue product and a
-- smallholder price food at what it cost to grow -- fundamentals that exist
-- independently of whatever the last trade happened to clear at.
local FOOD_PER_FARM_HAND = 0.70 * 6 + 0.25 * 3   -- 4.95, 5% crop failure
local FOOD_PER_FARM_TOOLED = 0.80 * 10 + 0.15 * 5 -- 8.75
local CLOTHES_PER_LABOR = 3 / 2                   -- MAKE_CLOTHES: 2 -> 3
local LABOR_PER_TOOL = 3                          -- CRAFT_TOOLS: 3 -> 1
-- Per PARCEL per tick, and per unit of labour, for the two land uses that
-- are not farming. Yields are set so one parcel serves about the same number
-- of people whichever way it is used (a field feeds 4.95/0.8 = 6.2), so these
-- are directly comparable to FOOD_PER_FARM_HAND when choosing what to build.
local SHELTER_PER_DWELLING = 6                    -- LET_DWELLING: 0.5 -> 6
local LABOR_PER_LET = 0.5
local ENERGY_PER_PLANT = 6                        -- GENERATE_POWER: 1 -> 6
local LABOR_PER_GENERATE = 1

-- The fixed real quantities a household turns over every tick -- what it
-- consumes plus what spoils on the shelf. These are the denominators that
-- turn cash into a money price per unit, and they are the reason the price
-- level has somewhere to sit (see "The nominal anchor" above).
local SUBSISTENCE_FOOD = 0.8    -- HUNGER quantity_per_tick
local COMFORT_CLOTHES = 0.2     -- COMFORT quantity_per_tick
local SHELTER_PER_TICK = 1      -- SHELTER quantity_per_tick
local ENERGY_PER_TICK = 1       -- POWER quantity_per_tick
local FOOD_DECAY = 0.3          -- FOOD decay_per_tick
local CLOTHES_DECAY = 0.05      -- CLOTHES decay_per_tick
local PLANNING_HORIZON = 20     -- ticks of savings a household spends against


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


local function running_on(parcel_id)
  for _, p in ipairs(ctx.processes) do
    if p.parcel_id == parcel_id then return true end
  end
  return false
end


local function farm_parcel()
  for _, p in ipairs(ctx.parcels) do
    for _, f in ipairs(p.facilities) do
      if f == "FARM" then return p.id end
    end
  end
  return nil
end


-- Every parcel of ours carrying `facility_type`, in ctx order (which is
-- creation order, so it is stable across ticks and across runs). Land stopped
-- being one field per firm when housing and power arrived: a firm can hold
-- several dwellings and work each of them, and facility capacity means each
-- one backs its own running process.
local function parcels_with(facility_type)
  local out = {}
  for _, p in ipairs(ctx.parcels) do
    for _, f in ipairs(p.facilities) do
      if f == facility_type then out[#out + 1] = p.id break end
    end
  end
  return out
end


-- Parcels with nothing standing on them yet -- the land the firm still gets
-- to decide about. One use per parcel is a rule of THIS script, not of the
-- engine: nothing stops a farm and a dwelling sharing an acre, so treating
-- "has any facility" as "spoken for" is what keeps the three uses genuinely
-- rival. See the note on the BUILD_ recipes in scenario.py.
-- Is a build already under way? Builds take several ticks and tie up the
-- parcel, and a firm that started one every tick it could afford would sink
-- its whole balance into simultaneous construction before the first one
-- finished and told it anything about whether the use was worth it.
local function building_now()
  for _, p in ipairs(ctx.processes) do
    if string.sub(p.recipe, 1, 6) == "BUILD_" then return true end
  end
  return false
end


local function bare_parcels()
  local out = {}
  for _, p in ipairs(ctx.parcels) do
    if #p.facilities == 0 then out[#out + 1] = p.id end
  end
  return out
end


-- Orders are good-til-cancelled: an unfilled one rests in the book until
-- cancelled or filled. Cancel everything this script placed last tick (the
-- entity's own events are all ctx.events contains) so exactly one generation
-- of orders is live at a time, and report how each one did on the way past.
--
-- Returns fills["SYMBOL|side"] = { ordered = q, filled = q, ratio = 0..1 }.
-- A key is ABSENT when no order was placed, and also when the order was
-- killed at the auction for want of funds or holdings -- that is a failure
-- to be solvent, not evidence about the price, and adapting on it would push
-- a broke agent to bid ever higher for what it already could not afford.
local function settle_last_orders()
  local by_order, killed, fills = {}, {}, {}

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
    if e.order_id and by_order[e.order_id] then
      if e.type == "trade" then
        local o = by_order[e.order_id]
        o.filled = o.filled + (tonumber(e.quantity) or 0)
      elseif e.type == "order_cancelled" then
        killed[by_order[e.order_id].key] = true
      end
    end
  end

  for _, o in pairs(by_order) do
    local f = fills[o.key] or { ordered = 0, filled = 0 }
    f.ordered = f.ordered + o.ordered
    f.filled = f.filled + o.filled
    fills[o.key] = f
  end

  for key, f in pairs(fills) do
    if killed[key] or f.ordered <= 0 then
      fills[key] = nil
    else
      f.ratio = f.filled / f.ordered
    end
  end

  return fills
end


-- How far this seller has conceded below its cost anchor on one symbol,
-- updated from last tick's fill ratio and persisted in ctx.state. Concedes
-- fast when stock will not move and firms up slowly when it all does; the
-- clamp is what keeps it a search rather than a spiral. Sell side only --
-- see "BOUNDED ADAPTATION" above for why buyers must not do this.
local function concede(fills, symbol)
  local factors = ctx.state.factors or {}
  local key = symbol .. "|sell"
  local factor = tonumber(factors[key]) or 1.0

  local f = fills[key]
  if f then
    -- A partial fill means the order was the marginal one at the clearing
    -- price and got rationed, which is still a reason to concede.
    local step = (f.ratio < 0.999) and -STEP_MISS or STEP_HIT
    factor = math.max(ADAPT_LO, math.min(ADAPT_HI, factor * (1 + step)))
  end

  factors[key] = factor
  ctx.state.factors = factors
  return factor
end


-- Quote helper: apply the bounded multiplier to a fundamental reservation
-- price, then clamp to a range the agent is genuinely willing to trade in.
-- `floor`/`ceiling` are the hard economic limits (a buyer's ability to pay,
-- a seller's opportunity cost); the multiplier only searches between them.
local function quote(anchor, factor, floor, ceiling)
  local price = anchor * factor
  if floor and price < floor then price = floor end
  if ceiling and price > ceiling then price = ceiling end
  if price < EPSILON then price = EPSILON end
  return price
end


-- Orders cross into Python as strings, so format them explicitly rather
-- than letting Lua's tostring pick a representation.
local function amount_str(x)
  return string.format("%.4f", x)
end
