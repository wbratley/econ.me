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

local food_price = market_price("FOOD", 3)
local clothes_price = market_price("CLOTHES", 8)
local tools_price = market_price("TOOLS", 20)
local shelter_price = market_price("SHELTER", 3)
local energy_price = market_price("ENERGY", 3)
local farm_yield = unlocked_agronomy and FOOD_PER_FARM_TOOLED or FOOD_PER_FARM_HAND

local dwellings = parcels_with("DWELLING")
local plants = parcels_with("POWER-PLANT")
local farms = parcels_with("FARM")

-- Firms are not identical: this is seeded per firm at genesis. Without it
-- every firm quotes the same number for the same tranche and the auction
-- rations them by whose order was created first, so the same firm wins the
-- scarce labor every tick forever -- a simulation artifact dressed up as a
-- competitive outcome.
local bid_factor = tonumber(ctx.state.bid_factor) or 1.0

-- Required gross margin, as a fraction of revenue. At 0 (the default, and
-- what every result before this reproduced) the firm bids labor at exactly
-- its marginal revenue product and prices output at exactly its labor cost.
-- Those are inverses, so the margin is zero BY CONSTRUCTION and every
-- friction -- food rotting unsold, crop failure, conceded asks -- comes
-- straight out of capital. Measured, that drains the sector's whole genesis
-- endowment by tick ~150.
--
-- The wedge is deliberately SYMMETRIC: bid x (1 - margin) below, ask
-- / (1 - margin) further down. That pairing matters, because the price level
-- in this economy is a free parameter -- nothing pins it, so a one-sided
-- wedge does not just change the margin, it makes prices drift forever.
-- Mark the bid down only, and the wage settles at w = y*P*(1-margin), so the
-- ask is w/y = P*(1-margin): next tick's price is (1-margin) times this
-- one's, forever -- geometric deflation.
-- Mark the ask up only and it inflates the same way. With both, the fixed
-- point is ask = w/y/(1-margin) = P, unchanged, while labor cost per unit of
-- output is P*(1-margin) -- a gross margin of exactly `margin` on revenue,
-- and no drift.
--
-- Applied uniformly to every tranche, not just the ones valued at a sale
-- price: the firm requires the same return on every hour it hires, and
-- exempting (say) the research tranche would just make research the one use
-- of labor the firm systematically overpays for.
local margin = tonumber(ctx.state.firm_margin) or 0
if margin < 0 or margin >= 1 then margin = 0 end
local markup = 1 / (1 - margin)

-- Per-firm RNG for investment timing. A fresh LuaRuntime is built every tick
-- (lua_engine), so Lua's math.random state never persists across ticks:
-- without reseeding from this stored seed every firm would draw the same
-- number every tick. Seeded once per firm at genesis (scenario.py) and
-- advanced here each tick, so the stream is deterministic given the scenario
-- seed yet differs across firms and ticks.
math.randomseed(tonumber(ctx.state.rng) or 1)
local invest_roll = math.random()
ctx.state.rng = math.random(1, 2000000000)

-- Persistent research-push timer: every 20 ticks, if enough LABOR is banked
-- and AGRONOMY isn't unlocked yet, spend a tick converting 6 LABOR into 6
-- LABOR-FARM (six back-to-back duration-0 conversions in one script call --
-- all resolve, priority-ordered, before the research/farm intents below
-- them), then spend 5 of it on research and farm with the 1 left over.
local research_timer = (ctx.state.research_timer or 0) + 1
ctx.state.research_timer = research_timer

-- Is an AGRONOMY research process already running? RESEARCH_AGRONOMY is now
-- paid as a FLOW -- 1 LABOR-FARM per tick for 5 ticks (per_tick_inputs) --
-- rather than the 5 LABOR-FARM lump it used to demand at start. The lump
-- was unreachable: it needed 5 LABOR-FARM held at once, but the labour goods
-- decay 0.5/tick so no stock accumulated, and the firm's whole inflow went
-- to ongoing farming anyway. The flow form lets the firm convert one
-- labour-hour to LABOR-FARM each tick and feed the running process at tick
-- step 7c, before decay takes it -- so research actually fires on its own.
local researching = false
for _, p in ipairs(ctx.processes) do
  if p.recipe == "RESEARCH_AGRONOMY" then researching = true end
end

-- Start research once the timer fires. No lump-sum labour gate (the old
-- `labor >= 6`, checked pre-auction/post-decay, was structurally never
-- true -- max firm receipt 2.5 against decay 0.5/tick). `not researching`
-- keeps one firm from starting a second while the first runs.
if (not unlocked_agronomy) and (not researching) and research_timer >= 20 then
  ctx.action.start_process("RESEARCH_AGRONOMY", nil, 15)
  ctx.state.research_timer = 0
  -- The per-tick draw runs THIS tick (step 7c, right after the process is
  -- created), so commit to feeding it now: the farm loop below reads this and
  -- skips one harvest, leaving the LABOR-FARM research draws. Without this the
  -- process would fail on its first tick every time -- it starts, the draw
  -- finds nothing, FAILED.
  researching = true
end

-- What to put on bare land. Hoisted ABOVE the farm loop on purpose:
-- BUILD_FARM pays its labour as a per-tick flow, so the process's FIRST draw
-- runs at step 7c of the very tick it is created. If the firm only learned it
-- was building *after* farming, every build would starve its first draw and
-- die -- a single missed per-tick draw fails the whole process. Deciding
-- first lets the farm loop idle one field THIS tick to feed it. The firm
-- prices each use by what the parcel earns per tick net of its labour;
-- whichever pays most wins the acre (a dwelling is a field not growing food,
-- and which gets built is an outcome of prices, not a genesis setting). One
-- build at a time (`not building_now()`), never below a positive net margin.
local wage = market_price("LABOR", 5)
local bare = bare_parcels()
local intended_build = nil
if #bare > 0 and not building_now() then
  local uses = {
    { recipe = "BUILD_FARM",        labor = 2,
      value = farm_yield * food_price - wage },
    { recipe = "BUILD_DWELLING",    labor = 4,
      value = SHELTER_PER_DWELLING * shelter_price - LABOR_PER_LET * wage },
    { recipe = "BUILD_POWER_PLANT", labor = 6,
      value = ENERGY_PER_PLANT * energy_price - LABOR_PER_GENERATE * wage },
  }
  table.sort(uses, function(a, b) return a.value > b.value end)
  local best = uses[1]
  if best.value * (1 - margin) > 0.01 and balance >= best.labor * wage then
    -- Real-options entry. Irreversible investment under uncertainty is not
    -- exercised the instant net value turns positive: firms wait for the
    -- surplus to clear a hurdle, and the more compelling the opportunity the
    -- shorter the wait. So the per-tick commit probability rises with the
    -- build's net surplus relative to its labour outlay -- near equilibrium
    -- (value ~ 0) it is ~0 (and the value>0 gate already enforces that),
    -- while when food is scarce and the parcel is a windfall it saturates
    -- toward 1 and the market pulls capital in quickly. This is the
    -- market-price decision (value folds in P_FOOD and wage) layered with a
    -- small random element (the per-firm die) -- the two ingredients of
    -- staggered entry in reality, not a fixed probability that waits as long
    -- for a marginal build as for a windfall.
    local commit_prob = 1 - math.exp(-best.value / (best.labor * wage))
    if invest_roll < commit_prob then
      intended_build = best
      ctx.action.start_process(best.recipe, bare[1], 29)
    end
  end
end
-- A build is active this tick if one is already running OR was just queued.
-- The farm loop reads this to leave one field's raw LABOR unconverted for the
-- per-tick draw at step 7c.
local building = building_now() or (intended_build ~= nil)

-- Work every field that is standing and idle, exactly as the dwelling and
-- plant loops below do. This used to farm `farm_parcel()` -- the FIRST field
-- only -- which capped the whole sector at one harvest per firm per tick
-- however much land it held: 19 standing farms produced 2.4 harvests a tick
-- between them, with spare labour on the market and ZERO rejections. The
-- fields were not failing to run, they were never being asked to.
--
-- Priority 23/24 puts farming BEHIND lets and generation, which is a change
-- of order and deliberate. While this was one field it did not matter; a
-- firm that can now spend every hour it owns on land would starve the
-- utilities from priority 10, which is the exact failure the note above the
-- dwelling loop records. It also agrees with the firm's own bid schedule: an
-- hour let is worth 6 SHELTER (0.2 -> 6), an hour generating 20 ENERGY
-- (0.3 -> 6), an hour farmed 4.95 FOOD. Land is the least valuable hour of
-- the three unless food is dear, so it goes last of the three.
--
-- WORK_AS_FARMER is duration-0 and completes inline, so each conversion is
-- in hand before the priority-24 farm intent that spends it.
-- Chase the highest-profit use of each labour-hour. The engine consumes held
-- labour in priority order (ascending), so priorities are set by value, not
-- by habit. Every idle field is still queued -- the engine retries a
-- start_process rejected for want of inputs after the auction, so over-asking
-- is both honest and the way a field gets fed by labour bought this tick --
-- but the contest that was stuck is farming against manufactures, and that
-- is settled by priority, not by gating. See the loom note below the loop.
--
-- Funding a running RESEARCH_AGRONOMY process works exactly like funding a
-- build: idle ONE field -- skip its conversion AND its harvest -- so the raw
-- LABOR that field would have absorbed survives to the process's per-tick
-- draw at step 7c. (Research used to draw LABOR-FARM and skip only the
-- harvest, but LABOR-FARM is fungible: when a labour shortfall left fewer
-- conversions succeeding than fields queued for harvest, those harvests ate
-- the research's LABOR-FARM before the draw. Raw LABOR + a skipped conversion
-- has no such leak.) The cost of R&D is one field's harvest a tick.
local skipped_for_research = false
local skipped_for_build = false
for _, pid in ipairs(farms) do
  if not running_on(pid) then
    -- While a BUILD_* or RESEARCH_AGRONOMY process runs, leave ONE field
    -- unconverted: the raw LABOR it would have absorbed stays held and feeds
    -- the process's per-tick draw at step 7c. Both investments consume raw
    -- LABOR (upstream of the conversion), so both skip the conversion itself.
    if building and not skipped_for_build then
      skipped_for_build = true
    elseif researching and not skipped_for_research then
      skipped_for_research = true
    else
      ctx.action.start_process("WORK_AS_FARMER", nil, 24)
      ctx.action.start_process(unlocked_agronomy and "FARM_FOOD_TOOLED" or "FARM_FOOD_HAND", pid, 25)
    end
  end
end
-- Produce to stock, not to spot price. Chasing the spot price made every
-- firm flip into the loom at once (cloth dear) and out at once (food short)
-- -- a cobweb violent enough to drive food output to zero some ticks. The
-- real-world damper is inventory: a firm asks not "which line pays more this
-- instant" but "have I banked enough food to spare an hour for the loom?".
-- FOOD here is perishable (30%/tick) and a single hand-harvest (~4.95) barely
-- covers one firm's share of the 24-food/tick economy demand, so most firms
-- are at subsistence and have NO surplus to divert -- which is the honest
-- reason cloth stays scarce until agronomy (4.95 -> 8.75) and extra fields
-- create real headroom. The loom runs at priority 23 only when food_stock
-- clears the SURPLUS floor, so the hour genuinely comes from headroom rather
-- than out of someone's mouth; otherwise every hour goes to food. Each firm
-- watches its OWN stock (which drifts apart across firms as their fields,
-- builds and sales differ), so the reallocation de-synchronises instead of
-- stampeding, and no firm drives its own food to zero.
local FOOD_BUFFER = 12      -- food banked above this (a real surplus, ~2+ harvests) = the firm can spare an hour for the loom
local CLOTHES_CAP = 6       -- don't pile cloth beyond this
local food_stock = holding_qty("FOOD")
local clothes_stock = holding_qty("CLOTHES")
-- The loom also yields while a RESEARCH process runs or a BUILD is underway:
-- both are committed investment that claim labour first, and a higher-priority
-- loom (23) would eat the labour before the research conversion (24, same as
-- the fields) can fund its per-tick draw -- so research would starve. The
-- firm parks cloth for the few ticks R&D or construction takes, the realistic
-- diversion of current production by investment. (The conversion stays at 24,
-- not earlier, on purpose: at a higher priority it would succeed at step 7 on
-- leftover labour and its LABOR-FARM would leak to the fields' harvests before
-- the 7c draw -- the skip-one-harvest logic only holds when conversions and
-- harvests resolve together at 7b.)
if food_stock >= FOOD_BUFFER and clothes_stock < CLOTHES_CAP
   and not building and not researching then
  ctx.action.start_process("MAKE_CLOTHES", nil, 23)
end
-- Tools are capital forged only from genuine food surplus (same SURPLUS
-- floor as the loom): the hour must come from headroom, not out of the field
-- that feeds someone. They run last, after farming.
if holding_qty("TOOLS") < 3 and food_stock >= FOOD_BUFFER then
  ctx.action.start_process("CRAFT_TOOLS", nil, 26)
end

-- Work every dwelling and every plant that is standing and idle. Facility
-- capacity means one facility backs one running process, so this is one
-- process per parcel: housing more people or generating more power is a
-- building decision, not a scheduling one.
-- Priority 21/22 puts these AHEAD of tools and clothes, and that ordering is
-- load-bearing rather than cosmetic. Intents consume the firm's labour in
-- priority order, so whatever runs last gets what is left -- and at 5 firms
-- needing 7.5 labour a tick against 30 units issued to the whole economy,
-- something always goes short. Measured with generation last: GENERATE_POWER
-- failed on all five plants nearly every tick for sixty ticks, energy output
-- flat zero against a standing demand of 30, while clothes were made on
-- schedule. An hour spent generating is worth six units of energy; an hour
-- spent on clothes is worth one and a half units of clothes. The bid schedule
-- below already says so; this makes the firm's own spending agree with it.
for _, pid in ipairs(dwellings) do
  if not running_on(pid) then ctx.action.start_process("LET_DWELLING", pid, 21) end
end
for _, pid in ipairs(plants) do
  if not running_on(pid) then ctx.action.start_process("GENERATE_POWER", pid, 22) end
end

-- Dividends: distribute real profit to the share register, never capital.
--
-- The reserve is the firm's genesis endowment, so only cash earned ABOVE
-- what it started with is distributable. That is not conservatism for its
-- own sake: at margin 0 firms decapitalise rather than accumulate (see the
-- note on `margin` above), and paying out of working capital would just
-- bankrupt them faster. What the rule captures there is the late phase --
-- once the sector consolidates, the survivor faces less competition for
-- labour, earns a genuine margin, and only then does capital income flow.
-- With a margin set, the same rule stops meaning "wait for a monopolist"
-- and starts meaning what it says: distribute profit, retain capital.
--
-- The register is read live (ctx.query.holders), not cached in state, so a
-- dividend follows the shares the moment any of them change hands.
local share_sym = ctx.state.share_symbol
if share_sym and share_sym ~= "" then
  local period = tonumber(ctx.state.dividend_period) or 0
  local timer = (ctx.state.dividend_timer or 0) + 1
  ctx.state.dividend_timer = timer
  if period > 0 and timer >= period then
    ctx.state.dividend_timer = 0
    local reserve = tonumber(ctx.state.firm_cash_reserve) or 0
    local payout = tonumber(ctx.state.dividend_payout) or 0
    local distributable = (balance - reserve) * payout
    if distributable > 0.01 then
      local register = ctx.query.holders(share_sym)
      local total_shares = 0
      for _, h in ipairs(register) do total_shares = total_shares + tonumber(h.quantity) end
      if total_shares > 0 then
        for _, h in ipairs(register) do
          local amount = distributable * tonumber(h.quantity) / total_shares
          if amount >= 0.0001 and h.account_id then
            ctx.action.transfer(account.id, h.account_id,
                                 string.format("%.4f", amount), "dividend", 3)
          end
        end
        -- Spend against what is left after the payout. `balance` was read at
        -- the start of the tick, and the dividend transfers resolve (priority
        -- 3) well before the auction settles this tick's labour orders.
        balance = balance - distributable
      end
    end
  end
end

-- Sell surplus output at cost, adapting downward if it doesn't move.
--
-- Marginal cost is the labor that went into it, valued at the going wage --
-- a real anchor, unlike marking down the last trade. Output is perishable,
-- so an ask that keeps failing to clear is worth conceding on: the bounded
-- multiplier in concede() lets the firm cut below cost rather than watch the
-- stock rot, but cannot compound its way to zero.
-- Sell all of it: FOOD rots 30%/tick, so any stock held back as a "buffer"
-- is food taken out of mouths and left to decay -- measured, that buffer
-- starved the population (firms banked ~24 food while hunger sat at 0.44).
-- Per-tick supply lumpiness from the farm/weave alternation is absorbed by
-- the market -- firms reach the surplus floor at different times as their
-- fields and builds differ -- not by hoarding a perishable.
local food = holding_qty("FOOD")
if food > 0.01 then
  local unit_cost = market_price("LABOR", 5) / farm_yield * markup
  ctx.action.place_order("FOOD", "sell", amount_str(food),
                          amount_str(quote(unit_cost, concede(fills, "FOOD"))),
                          account.id, 40)
end
local clothes = holding_qty("CLOTHES")
if clothes > 0.01 then
  local unit_cost = market_price("LABOR", 5) / CLOTHES_PER_LABOR * markup
  ctx.action.place_order("CLOTHES", "sell", amount_str(clothes),
                          amount_str(quote(unit_cost, concede(fills, "CLOTHES"))),
                          account.id, 41)
end

-- Rent and power. Both goods decay completely at the end of the tick, so
-- there is no such thing as holding them back for a better price -- an unsold
-- night's occupancy is gone. That makes conceding on them strictly correct
-- where it is merely reasonable for food, and it is why a landlord in this
-- economy has more reason to cut than a farmer does.
local shelter = holding_qty("SHELTER")
if shelter > 0.01 then
  local unit_cost = wage * LABOR_PER_LET / SHELTER_PER_DWELLING * markup
  ctx.action.place_order("SHELTER", "sell", amount_str(shelter),
                          amount_str(quote(unit_cost, concede(fills, "SHELTER"))),
                          account.id, 42)
end
local energy = holding_qty("ENERGY")
if energy > 0.01 then
  local unit_cost = wage * LABOR_PER_GENERATE / ENERGY_PER_PLANT * markup
  ctx.action.place_order("ENERGY", "sell", amount_str(energy),
                          amount_str(quote(unit_cost, concede(fills, "ENERGY"))),
                          account.id, 43)
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
-- for labor than the goods it makes are worth. With a margin the same
-- argument holds one step in: the reservation price is marginal revenue
-- product NET of the required margin, and bidding above it is precisely the
-- decapitalisation the margin exists to stop.
local tranches = {}

if field_id then
  -- One field runs one process at a time, so exactly one unit of labor a
  -- tick is worth the full farm yield to this firm. (Tooled farming used to
  -- burn 0.02 TOOLS per tick; that input was removed from FARM_FOOD_TOOLED
  -- so there is no tool-wear cost to subtract here anymore.)
  tranches[#tranches + 1] = { qty = 1, price = farm_yield * food_price }
end

-- One tranche per dwelling and per plant, not one for the sector: each
-- facility runs its own process, so the Nth dwelling's labour is worth
-- exactly what the Nth dwelling produces. Priced per unit of labour, which is
-- why letting -- half a unit of upkeep for six units of occupancy -- bids so
-- much harder per hour than farming does.
for _ = 1, #dwellings do
  tranches[#tranches + 1] = {
    qty = LABOR_PER_LET,
    price = SHELTER_PER_DWELLING * shelter_price / LABOR_PER_LET,
  }
end
for _ = 1, #plants do
  tranches[#tranches + 1] = {
    qty = LABOR_PER_GENERATE,
    price = ENERGY_PER_PLANT * energy_price / LABOR_PER_GENERATE,
  }
end

tranches[#tranches + 1] = { qty = 2, price = CLOTHES_PER_LABOR * clothes_price }

if holding_qty("TOOLS") < 3 then
  tranches[#tranches + 1] = { qty = LABOR_PER_TOOL, price = tools_price / LABOR_PER_TOOL }
end

-- Feed the running build's per-tick draw. Fires every tick a build is
-- active (running OR just queued), not only the tick it was queued: the
-- per-tick draw runs at step 7c of EVERY tick the process is alive, and a
-- build that draws labour only on its first tick starves on the second and
-- dies (measured before this: 99 of 111 builds failed on their 2nd draw).
-- One unit covers the 0.67-LABOR/tick draw with margin; the farm-loop skip
-- (above) keeps a conversion from reclaiming it before the draw runs. Valued
-- at a farm hour -- the same conservative floor the research push uses.
if building then
  tranches[#tranches + 1] = { qty = 1, price = farm_yield * food_price }
end
-- Same for a running RESEARCH process: buy one raw LABOR to cover its 1/tick
-- draw. The farm-loop skip keeps a field's conversion from reclaiming it
-- before the 7c draw; the tranche makes sure the labour is actually bought.
if researching then
  tranches[#tranches + 1] = { qty = 1, price = farm_yield * food_price }
end

table.sort(tranches, function(a, b) return a.price > b.price end)

-- The margin is withheld here rather than inside each tranche so it applies
-- to every use of labor by construction, and so a tranche added later cannot
-- quietly opt out of it.
local spend_left = balance
for _, t in ipairs(tranches) do
  local price = t.price * bid_factor * (1 - margin)
  if price >= 0.01 then
    local qty = math.min(t.qty, spend_left / price)
    if qty > 0.01 then
      ctx.action.place_order("LABOR", "buy", amount_str(qty), amount_str(price),
                              account.id, 45)
      spend_left = spend_left - qty * price
    end
  end
end
