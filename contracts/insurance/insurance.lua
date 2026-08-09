-- contracts/insurance/insurance.lua
-- ===========================================================================
-- Insurance -- the trigger-and-pay engine (Step 5d reference contract).
-- ===========================================================================
-- Bind this script to the INSURER as POLICY (not BEHAVIOUR). A BEHAVIOUR
-- script sees only ITS OWN entity's events; the insurer must watch its
-- POLICYHOLDERS' events, and only a POLICY script sees every entity's
-- events. This is the contract that validates ctx.events as a trigger source
-- -- the one engine affordance no earlier contract exercises.
--
-- Each tick this script does the insurer's whole back office:
--   1. SCAN ctx.events for the trigger event type (default
--      entity_incapacitated -- a death benefit). For each event whose
--      entity_id is a policyholder, mark that policy TRIGGERED.
--   2. PAY triggered-but-unpaid claims: a real ctx.action.transfer from the
--      risk pool to the beneficiary.
--
-- WHY PAYOUT IS LUA-DRIVEN (unlike futures' Python settle). Futures' settle
-- needs try/except branching (seize, and if the defaulter has no goods, the
-- winner takes a haircut) that deferred Lua-intent resolution cannot express.
-- Insurance payout has NO such branching: a LOCAL POOL COUNTER prevents
-- over-commitment (it tracks the remaining balance, decrementing as it queues
-- payouts, so the pool is never overdrawn), and the coverage oracle (written
-- at underwriting) matches the payout amount exactly, so the coverage-cap
-- VALIDATOR cannot veto. With both guards, the queued transfer cannot fail --
-- so marking paid before resolution is safe. This is the cleanest possible
-- demonstration of event -> action.
--
-- TRIGGER MATCHING. The default trigger is entity_incapacitated: a
-- policyholder crossing an incapacitating condition threshold (conditions.py)
-- emits that event; this script sees it next tick and pays. Matching is on
-- event type == trigger AND event.entity_id == policyholder. Other triggers
-- (decay, need_unmet, ...) work the same way; richer matching (on symbol,
-- quantity) is an extension.
--
-- state shape (written by contracts.insurance.insurance; triggered/paid owned
-- here):
--   state.policies[POLICYHOLDER_ID] = {
--     beneficiary_account_id = "<acct>",
--     coverage   = "1000",
--     premium    = "50",
--     term       = 20,            -- nil = perpetual
--     issued_tick = 1,
--     triggered  = false,         -- OWNED by this script
--     trigger_tick = nil,         -- OWNED by this script
--     paid       = false,         -- OWNED by this script
--   }
-- ===========================================================================

local function money(x)
  return string.format("%.4f", x)
end

local trigger = ctx.state.trigger or "entity_incapacitated"

-- 1. Detect triggers: scan last tick's events for the trigger type.
for _, e in ipairs(ctx.events) do
  if e.type == trigger then
    local pol = ctx.state.policies[e.entity_id]
    if pol ~= nil and pol.triggered ~= true then
      pol.triggered = true
      pol.trigger_tick = ctx.tick
    end
  end
end

-- 2. Pay triggered-but-unpaid claims. A local counter tracks the pool's
--    remaining balance so the script never queues more than the pool can
--    cover (safe under deferred intent resolution).
local remaining = tonumber(ctx.query.balance(ctx.state.pool_account_id))
local total = 0

for pid, pol in pairs(ctx.state.policies) do
  -- Stamp total in-force coverage (for observation / the coverage ratio).
  if pol.term == nil or ctx.tick <= tonumber(pol.term) then
    total = total + tonumber(pol.coverage)
  end
  -- Pay if triggered, unpaid, in term, and the pool can cover it.
  if pol.triggered == true and pol.paid ~= true then
    local in_term = pol.term == nil or ctx.tick <= tonumber(pol.term)
    local cov = tonumber(pol.coverage)
    if in_term and remaining >= cov then
      ctx.action.transfer(
        ctx.state.pool_account_id,
        pol.beneficiary_account_id,
        pol.coverage,
        "insurance-payout:" .. pid)
      pol.paid = true
      remaining = remaining - cov
    end
  end
end

ctx.state.total_coverage = money(total)
