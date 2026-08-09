-- contracts/insurance/coverage_cap.lua
-- ===========================================================================
-- Coverage cap -- constitutional constraint on the risk pool (Step 5d).
-- ===========================================================================
-- Bind this as a VALIDATOR on the INSURER entity. It fires only for the
-- insurer's OWN outbound transfers (a transfer's op.entity_id is the
-- sender's). A premium (policyholder -> pool) sends as the policyholder, so
-- this never gates cashing IN -- only draining the pool OUT.
--
-- It enforces the rule that a payout must be backed by DOCUMENTED COVERAGE,
-- and must not exceed it: the insurer may not pay a beneficiary more than the
-- coverage underwritten for them, and may not pay an undocumented beneficiary
-- at all. This is the insurance analogue of the loan's usury cap and the
-- futures' margin-sufficiency check -- a constitutional backstop that makes a
-- rogue insurer's naked payout fail-closed. The risk pool is locked to its
-- payouts.
--
-- THE ORACLE. A VALIDATOR has only its OWN state + queries -- it cannot read
-- the insurer's POLICY script state where the policy book lives. So the
-- coverage is mirrored into a queryable WorldSetting (the 5c signal pattern,
-- exactly as the loan's usury cap mirrors the loan book and the futures'
-- deficiency mirrors the margin book), keyed by the beneficiary's account:
-- insurance:coverage:<ACCT_ID> = {max = "<coverage>"}. underwrite() writes it
-- at policy creation; this validator reads it. No oracle row -> the payout is
-- undocumented -> veto. An amount over the documented max -> veto.
-- ===========================================================================

if ctx.op.type ~= "transfer" then
  return true
end

local oracle = ctx.query.world_setting(
  "insurance:coverage:" .. (ctx.op.to_account_id or ""))
if oracle == nil then
  return {allow = false,
          reason = "risk-pool payout to undocumented beneficiary "
                   .. (ctx.op.to_account_id or "?")}
end

local paying = tonumber(ctx.op.amount)
local max = tonumber(oracle.max or "0")
-- A sliver of tolerance for 4dp quantisation.
if paying > max + 0.0001 then
  return {allow = false,
          reason = "payout " .. string.format("%.4f", paying)
                   .. " exceeds coverage " .. string.format("%.4f", max)}
end
return true
