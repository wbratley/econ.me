-- contracts/bond/monetization_cap.lua
-- ===========================================================================
-- Constitutional guard on the two-tier-money boundary a bond rests on.
-- ===========================================================================
-- A bond sale is a transfer of *existing* money (it creates none); only
-- issue_money creates base money (central-bank reserves). This VALIDATOR
-- caps how much base money the monetary authority may create in a single op.
-- At CAP = "0" it is a hard-money standard: bonds must be serviced from
-- existing funds, never by monetising the debt -- exactly the discipline a
-- bond-issuing government may want bound at the constitutional tier.
--
-- The cap is a constant in the source on purpose. Changing it is a
-- constitutional act (re-enact via set_validator at supermajority), the same
-- way fiscal_policy's rates are votable data -- decision rules are code
-- here, effect mechanisms are engine. A data-driven cap keyed off a
-- WorldSetting is the natural Step-5c upgrade once ctx.query.world_setting
-- ships; until then a re-enact is the governed path to retune it.
-- ===========================================================================

local CAP = "0"   -- "0" forbids issuance entirely; raise to permit some.

if ctx.op.type ~= "issue_money" then
  return true
end
if tonumber(ctx.op.amount) > tonumber(CAP) then
  return {allow = false,
          reason = "monetization cap breached: issue " .. ctx.op.amount .. " > " .. CAP}
end
return true
