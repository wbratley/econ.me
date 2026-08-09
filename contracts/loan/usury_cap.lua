-- contracts/loan/usury_cap.lua
-- ===========================================================================
-- Usury cap -- constitutional constraint on predatory lending (Step 5d).
-- ===========================================================================
-- Bind this as a VALIDATOR on the LENDER entity. It fires only for the
-- lender's OWN levies (a levy's op.entity_id is the levying authority's, and
-- _applicable_scripts keeps an entity-bound validator to its entity). It
-- enforces a statutory ceiling on collectible interest: the lender may levy no
-- more than principal + (principal * cap * elapsed) - already paid, regardless
-- of what the loan book says it is owed. A loan written above the cap can
-- still exist, but the excess interest is UNCOLLECTIBLE by force -- the
-- lender's levy is vetoed and it must fall back to foreclosing the collateral.
--
-- This is a usury law in the honest, enforceable sense. The engine cannot stop
-- two parties from WRITING a 50%-per-tick loan (a loan is a transfer + a book
-- entry, neither of which fires a validator); it can only stop the lender from
-- using the state's levy power to COLLECT usurious interest. The cap bites at
-- the enforcement boundary -- the one place the engine's privilege touches a
-- private debt. (A voluntary repayment at any rate is the borrower's own money
-- moving of its own accord; the engine has no grounds to stop it. Only
-- compelled collection -- levy -- is the state's act, and only the state's act
-- can be constitutionally capped.)
--
-- THE ORACLE. A VALIDATOR has only its OWN state + queries -- it cannot read
-- the lender's BEHAVIOUR script state where the loan book lives. So the loan's
-- cap-relevant terms are mirrored into a queryable WorldSetting (the 5c signal
-- pattern), keyed by the borrower's settlement account, maintained by the
-- Python helpers. The validator reads it via ctx.query.world_setting. This is
-- the same data-vs-mechanism split as fiscal policy: the loan is data (here,
-- an oracle row); the levy is the mechanism the cap gates.
--
-- The cap is two-layered (the decision-rule/data-effect split):
--   * a DEFAULT_CAP in source -- changing it is a constitutional act
--     (re-enact via set_validator at supermajority);
--   * a governed override read live each op from the "loan:usury_cap"
--     WorldSetting ({"rate": "<fraction>"}) via ctx.query.world_setting (5c).
-- ===========================================================================

local DEFAULT_CAP = "0.05"   -- 5% of principal per tick: the statutory ceiling.

if ctx.op.type ~= "levy" then
  return true
end

-- The loan's terms from the oracle. No oracle row -> not one of this lender's
-- loans, or already settled and cleared: allow.
local oracle = ctx.query.world_setting("loan:account:" .. ctx.op.from_account_id)
if oracle == nil then return true end

local setting = ctx.query.world_setting("loan:usury_cap")
local cap = tonumber((setting ~= nil and setting.rate) or DEFAULT_CAP)

local principal = tonumber(oracle.principal)
local paid = tonumber(oracle.paid or "0")
local elapsed = ctx.tick - tonumber(oracle.issue_tick or "0")
if elapsed < 0 then elapsed = 0 end
-- The legal maximum the lender may compel: principal back, plus interest
-- capped at the statutory rate, minus anything already paid. The loan's own
-- rate is irrelevant -- the cap is a ceiling, not a mirror.
local legal_max = principal + principal * cap * elapsed - paid

local levying = tonumber(ctx.op.amount)
-- A cent of tolerance: never veto a levy that is at (essentially) the cap.
if levying > legal_max + 0.01 then
  return {allow = false,
          reason = "usury cap: levy " .. string.format("%.4f", levying)
                   .. " exceeds legal max " .. string.format("%.4f", legal_max)
                   .. " (cap " .. string.format("%.4f", cap) .. "/tick)"}
end
return true
