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
  local reserved = 0
  for _, pid in ipairs(dwellings) do
    if not running_on(pid) then reserved = reserved + LABOR_PER_LET end
  end
  for _, pid in ipairs(plants) do
    if not running_on(pid) then reserved = reserved + LABOR_PER_GENERATE end
  end
  -- Queued for every idle field without checking the labour on hand, exactly
  -- as the dwelling and plant loops do. `labor` is read when the script runs,
  -- which is BEFORE this tick's auction, so it counts only what was bought
  -- last tick -- gating on it made the firm ask for work it could already pay
  -- for rather than work it was about to. That capped output at ~2.5 harvests
  -- a tick across 17 fields even after the one-field bug was gone. The engine
  -- retries a start_process rejected for want of inputs after clearing, so
  -- asking for every field and letting the market decide is now both the
  -- honest statement of intent and the one that gets fed.
  for _, pid in ipairs(farms) do
    if not running_on(pid) then
      ctx.action.start_process("WORK_AS_FARMER", nil, 23)
      reserved = reserved + 1
      ctx.action.start_process(unlocked_agronomy and "FARM_FOOD_TOOLED" or "FARM_FOOD_HAND", pid, 24)
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

-- What to put on bare land.
--
-- The firm prices each use the same way it prices labour: by what the parcel
-- would earn per tick, net of the labour it takes to work it. Whichever pays
-- most wins the acre. That is the whole point of the fixed land pool -- a
-- dwelling is a field that is not growing food, and which one gets built is
-- an outcome of prices rather than a setting in genesis.
--
-- Two deliberate conservatisms. Rent per tick is compared against a build
-- that costs labour once, so the firm is comparing a flow to a stock and will
-- always build if any use is profitable at all; the brake is that it only
-- ever starts ONE build at a time, so over-building takes many ticks and the
-- prices it is reading move underneath it. And it will not build while it
-- cannot cover the labour, which is what stops a broke firm sinking its last
-- cash into a hole in the ground.
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
  -- Net of the margin, like every other use of labour: a firm that builds at
  -- exactly break-even is decapitalising itself one acre at a time.
  if best.value * (1 - margin) > 0.01 and balance >= best.labor * wage then
    intended_build = best
    -- Only start it once the labour is actually in hand. start_process
    -- consumes its inputs at start, so firing this while short just fails the
    -- intent silently, every tick, forever -- which is exactly what happened
    -- the first time and left every parcel bare for a whole run. The labour
    -- is bid for below, so the build lands a tick or two after the decision.
    -- Queued whether or not the labour is in hand yet, for the same reason the
    -- farm loop no longer checks: `labor` is read before this tick's auction,
    -- so it is last tick's leftovers after a round of decay. A build wants 4-6
    -- units at once and lets, generation and farming are served first, so a
    -- firm essentially never holds that much when its script runs -- building
    -- stopped at tick 8 of 150 and left 6 parcels bare for the rest of the run.
    --
    -- The gate was right when it was written: start_process consumed inputs at
    -- start, so firing while short failed silently every tick forever, which is
    -- what left every parcel bare the first time. The engine now retries an
    -- input-starved start_process after clearing, and the build labour is bid
    -- for below, so the ask lands rather than evaporating. `building_now()`
    -- still holds this to one build at a time, which is what keeps it from
    -- becoming the spending spree that gate was guarding against.
    ctx.action.start_process(best.recipe, bare[1], 29)
  end
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
  -- tick is worth the full farm yield to this firm. Tooled farming also
  -- burns a little of the tool stock.
  local wear = unlocked_agronomy and (0.02 * tools_price) or 0
  tranches[#tranches + 1] = { qty = 1, price = farm_yield * food_price - wear }
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

-- Labour to put up whatever the firm decided to build. Without this tranche
-- the firm decides to build every tick and never has the hours to do it: the
-- build recipes want 2-6 LABOR held at once, and ordinary operations bid for
-- one or two. Valued at a farm hour, the same conservative floor the research
-- push uses -- the honest value is the discounted stream the building would
-- earn, and pricing it that way would have the firm outbid the entire economy
-- for construction labour.
if intended_build then
  local shortfall = intended_build.labor - labor
  if shortfall > 0 then
    tranches[#tranches + 1] = { qty = shortfall, price = farm_yield * food_price }
  end
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
