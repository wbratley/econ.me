-- contracts/futures/futures.lua
-- ===========================================================================
-- Futures exchange -- mark-to-market engine (Step 5d reference contract).
-- ===========================================================================
-- A futures position here is DATA + this BEHAVIOUR script (design.md §2). Bind
-- this script to the EXCHANGE (the CCP). Each tick it does the exchange's back
-- office:
--   * reads the signal price for each position's symbol and MARKS TO MARKET,
--   * flags BREACH (a credit below maintenance margin) and EXPIRY,
--   * stamps the exchange's total open interest for observation.
--
-- It does NOT settle. Settlement (payout + deficiency seizure) is a discrete
-- act -- done by contracts.futures.futures.settle() in Python, because the
-- deficiency case needs try/except branching ("seize, and if the defaulter
-- has no goods, the winner takes a haircut") that the engine's deferred
-- Lua-intent resolution cannot express: a script queues intents that resolve
-- only AFTER it returns, so it cannot branch on an outcome it cannot yet see.
-- (Contrast the bank, whose bookkeeping is all state mutation -- no branching
-- needed, so bank.lua does it inline.) Both halves of settlement go through
-- services.transfer / services.seize, which fire validators -- so a
-- margin-sufficiency cap gates the seizure regardless of the call path.
--
-- THE SIGNAL (Step 5c). The price comes from a WorldSetting oracle --
-- futures:price:SYMBOL = {price = "..."} -- posted by the platform between
-- ticks. The engine does not invent prices; it reads them. A missing signal
-- (nil) is a DARK FEED: the position is not marked this tick (its credits hold
-- stale; status unchanged). Mark-to-market is computed CUMULATIVELY from the
-- contract price each tick (not incrementally), so it is inherently skip-safe
-- -- a budget skip or a dark feed loses nothing; the next lit tick catches up.
--
-- Mark-to-market is a PURE BOOK UPDATE: the margin pool is commingled cash in
-- the exchange's account; the per-side credits are just numbers in state. No
-- money moves (exactly like the bank's intra-bank pay). The credits sum to the
-- posted pool for every position, always.
--
-- state shape (written by contracts.futures.futures; credits marked here):
--   state.positions[PID] = {
--     long         = "<eid>",
--     short        = "<eid>",
--     symbol       = "GRAIN",
--     quantity     = "100",
--     price        = "5.00",            -- contract price
--     expiry       = 10,                -- absolute tick
--     long_margin  = "100",             -- initial margin (constant)
--     short_margin = "100",
--     long_credit  = "110.0000",        -- OWNED by this script
--     short_credit = "90.0000",
--     last_mark    = 2,                 -- advanced by this script
--     status       = "open",            -- open | breached | expired | settled
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
      local contract = tonumber(p.price)
      local qty = tonumber(p.quantity)
      local lm = tonumber(p.long_margin)
      local sm = tonumber(p.short_margin)
      -- Mark to market, cumulative from the contract price (skip-safe).
      local long_credit = lm + (s - contract) * qty
      local short_credit = sm + (contract - s) * qty
      p.long_credit = money(long_credit)
      p.short_credit = money(short_credit)
      p.last_mark = ctx.tick
      -- Flag expiry first (a settled-ready position), then breach.
      if ctx.tick >= tonumber(p.expiry) then
        p.status = "expired"
      elseif long_credit < lm * maint or short_credit < sm * maint then
        p.status = "breached"
      else
        p.status = "open"
      end
    end
    total = total + tonumber(p.quantity)
  end
end

ctx.state.total_open_interest = money(total)
