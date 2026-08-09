-- contracts/option/option.lua
-- ===========================================================================
-- Options exchange -- mark-to-market engine (Step 5d reference contract).
-- ===========================================================================
-- An options position here is DATA + this BEHAVIOUR script (design.md §2). Bind
-- this script to the EXCHANGE (the CCP). Each tick it does the exchange's back
-- office:
--   * reads the signal price for each position's symbol and MARKS TO MARKET,
--   * flags BREACH (the writer's credit below maintenance) and EXPIRY,
--   * stamps the exchange's total open interest for observation.
--
-- It does NOT settle. Settlement (payout + deficiency seizure) is a discrete
-- act -- done by contracts.option.option.settle() in Python, because the
-- deficiency case needs try/except branching ("seize, and if the writer has no
-- goods, the buyer takes a haircut") that the engine's deferred Lua-intent
-- resolution cannot express: a script queues intents that resolve only AFTER it
-- returns, so it cannot branch on an outcome it cannot yet see. (Contrast
-- insurance, whose payout has no branching -- a local pool counter prevents
-- over-commit -- so insurance.lua drives payout inline.) Both halves of
-- settlement go through services.transfer / services.seize, which fire
-- validators -- so an option-sufficiency cap gates the seizure regardless of
-- the call path.
--
-- THE SIGNAL (Step 5c). The price comes from the SAME oracle a future reads:
-- futures:price:SYMBOL = {price = "..."} -- posted by the platform between
-- ticks. The underlying's price is shared infrastructure; a future and an
-- option on GRAIN read the same number. A missing signal (nil) is a DARK FEED:
-- the position is not marked this tick (its values hold stale; status
-- unchanged). Mark-to-market is computed CUMULATIVELY from the strike each
-- tick (not incrementally), so it is inherently skip-safe -- a budget skip or
-- a dark feed loses nothing; the next lit tick catches up.
--
-- THE ASYMMETRY. A future marks BOTH sides (long_credit, short_credit) because
-- both are obligated. An option marks the BUYER's intrinsic value and the
-- WRITER's credit (margin minus that value) because only the writer is
-- obligated. The buyer's "credit" IS the intrinsic value -- the worth of the
-- right. The writer's credit is what they would get back if settled now.
--
--   call intrinsic  = max(0, signal - strike) * qty   (right to BUY at strike)
--   put  intrinsic  = max(0, strike - signal) * qty   (right to SELL at strike)
--
-- Mark-to-market is a PURE BOOK UPDATE: the margin pool is the writer's posted
-- cash; the per-side values are just numbers in state. No money moves (exactly
-- like the bank's intra-bank pay and the futures' credit update).
--
-- state shape (written by contracts.option.option; values marked here):
--   state.positions[PID] = {
--     kind          = "call",            -- call | put
--     buyer         = "<eid>",           -- holder of the right
--     writer        = "<eid>",           -- the obligated party
--     symbol        = "GRAIN",
--     quantity      = "100",
--     strike        = "5.00",            -- exercise price
--     premium       = "50",             -- paid buyer -> writer at open
--     margin        = "200",            -- writer's posted collateral
--     expiry        = 10,               -- absolute tick
--     buyer_value   = "100.0000",       -- OWNED by this script
--     writer_credit = "100.0000",       -- OWNED by this script
--     last_mark     = 2,                -- advanced by this script
--     status        = "open",           -- open | breached | expired | settled
--   }
-- ===========================================================================

local function money(x)
  return string.format("%.4f", x)
end

local maint = tonumber(ctx.state.maintenance_fraction or "0.5")
local total = 0

for pid, p in pairs(ctx.state.positions or {}) do
  if p.status ~= "settled" then
    local feed = ctx.query.world_setting("futures:price:" .. p.symbol)
    if feed ~= nil then
      local s = tonumber(feed.price)
      local k = tonumber(p.strike)
      local qty = tonumber(p.quantity)
      local margin = tonumber(p.margin)
      -- Intrinsic value: what the option is worth if exercised now.
      local diff
      if p.kind == "call" then
        diff = s - k          -- right to BUY at strike (profits when price rises)
      else
        diff = k - s          -- right to SELL at strike (profits when price falls)
      end
      local intrinsic = 0
      if diff > 0 then intrinsic = diff * qty end
      p.buyer_value = money(intrinsic)
      p.writer_credit = money(margin - intrinsic)
      p.last_mark = ctx.tick
      -- Flag expiry first, then breach (a writer whose collateral is thin).
      if ctx.tick >= tonumber(p.expiry) then
        p.status = "expired"
      elseif (margin - intrinsic) < margin * maint then
        p.status = "breached"
      else
        p.status = "open"
      end
    end
    total = total + tonumber(p.quantity)
  end
end

ctx.state.total_open_interest = money(total)
