-- contracts/loan/loan.lua
-- ===========================================================================
-- Secured loan -- servicing engine (Step 5d reference contract).
-- ===========================================================================
-- A loan here is DATA + this BEHAVIOUR script (design.md §2). Bind this
-- script to the LENDER. Each tick it does the lender's back office:
--   * accrues simple interest on every outstanding loan,
--   * marks a loan DEFAULT at maturity if it remains unpaid,
--   * stamps the lender's total outstanding book for observation.
--
-- It does NOT enforce. Enforcement (levy / seize on default) is a discrete
-- legal act -- a foreclosure -- done by contracts.loan.loan.enforce() in
-- Python, because it needs try/except branching ("levy, and if short, seize")
-- that the engine's deferred Lua-intent resolution cannot express: a script
-- queues intents that resolve only AFTER it returns, so it cannot branch on
-- an outcome it cannot yet see. (Contrast the bank, whose bookkeeping is all
-- state mutation -- no branching needed, so bank.lua does it inline.) Both
-- halves of enforcement go through services.levy / services.seize, which fire
-- validators -- so a usury cap gates the levy regardless of the call path.
--
-- The clock is ctx.tick (Step 5a), NOT a run-counter, so a compute-budget
-- skip does not lose interest: elapsed catches up next run.
--
-- state shape (written by contracts.loan.loan; interest accrued here):
--   state.loans[EID] = {
--     account_id        = "<settlement account>",  -- what gets levied
--     principal         = "100",                   -- disbursed (money string)
--     rate              = "0.02",                  -- per-tick interest fraction
--     interest_due      = "4.0000",                -- OWNED by this script
--     issue_tick        = 0,
--     maturity          = 5,                       -- absolute due tick
--     last_accrued_tick = 2,                       -- advanced by this script
--     paid              = "0",                     -- principal+interest repaid
--     collateral        = {symbol="GRAIN", quantity="50"},  -- or null
--     status            = "active",                -- active | default | settled
--     default_tick      = null,                    -- set by this script
--   }
-- ===========================================================================

-- Account precision is Numeric(18,4); %.4f absorbs float dust and yields a
-- string Decimal() parses exactly.
local function money(x)
  return string.format("%.4f", x)
end

local total = 0

for borrower, loan in pairs(ctx.state.loans or {}) do
  if loan.status == "active" or loan.status == "default" then
    -- 1. Accrue simple interest on the outstanding principal (skip-safe:
    --    elapsed catches up after a budget skip). Interest accrues until the
    --    loan settles, so a defaulted loan keeps compounding late fees.
    local last = loan.last_accrued_tick or ctx.tick
    local elapsed = ctx.tick - last
    if elapsed > 0 then
      local principal = tonumber(loan.principal)
      local rate = tonumber(loan.rate)
      loan.interest_due = money(tonumber(loan.interest_due)
                                + principal * rate * elapsed)
      loan.last_accrued_tick = ctx.tick
    end

    -- 2. Mark default at maturity if still unpaid. Foreclosure itself is the
    --    operator's/script-caller's deliberate act (enforce()); this only
    --    flags that the loan has come due and gone unsatisfied.
    if loan.status == "active" and ctx.tick >= tonumber(loan.maturity) then
      local owed = tonumber(loan.principal) + tonumber(loan.interest_due)
                  - tonumber(loan.paid or "0")
      if owed > 0 then
        loan.status = "default"
        loan.default_tick = ctx.tick
      else
        loan.status = "settled"   -- matured and already covered: clean exit
      end
    end
  end

  if loan.status ~= "settled" then
    total = total + tonumber(loan.principal) + tonumber(loan.interest_due)
             - tonumber(loan.paid or "0")
  end
end

-- 3. Stamp the lender's at-risk book for observation.
ctx.state.total_outstanding = money(total)
