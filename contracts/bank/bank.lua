-- contracts/bank/bank.lua
-- ===========================================================================
-- Commercial bank -- servicing engine (Step 5d reference, two-tier money).
-- ===========================================================================
-- A bank here is DATA + this BEHAVIOUR script (design.md §2: an instrument
-- is no more an engine feature than a tax schedule). Bind this script to the
-- BANK entity. Each tick it does the bank's back office:
--   * accrues simple interest on every outstanding loan,
--   * reconciles the books -- stamps reserves / total deposits / the reserve
--     ratio -- so the bank's position is observable without recomputation.
--
-- The two-tier model (the whole point): the bank's RESERVE account is base
-- money (a real engine Account); its DEPOSIT balances are a shadow ledger in
-- this script's state -- claims on the bank, created by lending, NOT base
-- money. This script never calls issue_money; it never creates base money.
-- Lending (in contracts.bank.bank.lend) credits a deposit by writing a book
-- entry, and that is where money is created. This script only accrues and
-- reports. Credit money is a book, not a ledger feature.
--
-- The clock is ctx.tick (Step 5a), NOT a run-counter, so a compute-budget
-- skip does not lose interest: elapsed catches up next run.
--
-- state shape (written by contracts.bank.bank; interest accrued here):
--   state.currency       = "USD"
--   state.default_rate   = "0.01"
--   state.deposits[EID]  = "amount"                       -- shadow ledger
--   state.loans[EID] = {
--     principal        = "300",   -- original principal (money string)
--     rate             = "0.01",  -- per-tick interest fraction
--     interest_due     = "0",     -- accrued interest (OWNED by this script)
--     last_accrued_tick = 5,      -- advanced by this script
--     paid             = "0",     -- principal+interest repaid (money string)
--     repaid           = false,   -- fully settled?
--   }
--   state.reserves / total_deposits / reserve_ratio  -- stamped here (read-only)
-- ===========================================================================

-- Account precision is Numeric(18,4); %.4f absorbs float dust and yields a
-- string Decimal() parses exactly.
local function money(x)
  return string.format("%.4f", x)
end

-- The bank's reserve account in its currency.
local function reserve_account()
  for _, a in ipairs(ctx.accounts) do
    if a.currency == ctx.state.currency then return a.id end
  end
  return nil
end

-- 1. Accrue simple interest on every outstanding loan (skip-safe: elapsed
--    catches up after a budget skip, paying the missed ticks in one go).
for borrower, loan in pairs(ctx.state.loans or {}) do
  if not loan.repaid then
    local last = loan.last_accrued_tick or ctx.tick
    local elapsed = ctx.tick - last
    if elapsed > 0 then
      local principal = tonumber(loan.principal)
      local rate = tonumber(loan.rate)
      loan.interest_due = money(tonumber(loan.interest_due)
                                + principal * rate * elapsed)
      loan.last_accrued_tick = ctx.tick
    end
  end
end

-- 2. Reconcile the books: stamp the bank's position for observation.
local acct = reserve_account()
local reserves = acct and tonumber(ctx.query.balance(acct)) or 0
local total_dep = 0
for _, bal in pairs(ctx.state.deposits or {}) do
  total_dep = total_dep + tonumber(bal)
end
ctx.state.reserves = money(reserves)
ctx.state.total_deposits = money(total_dep)
ctx.state.reserve_ratio = total_dep > 0 and money(reserves / total_dep) or nil
