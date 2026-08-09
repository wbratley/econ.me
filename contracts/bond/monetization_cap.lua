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
-- The cap has two layers (the decision-rule/data-effect split):
--   * a DEFAULT in source -- changing it is a constitutional act (re-enact
--     via set_validator at supermajority), the same way fiscal_policy's
--     rates are votable data. Decision rules are code here.
--   * a governed override keyed off the WorldSetting "monetary:issue_cap"
--     (a signal, Step 5c Fork A) -- retuning the cap becomes writing data,
--     read live each op via ctx.query.world_setting. Effect mechanisms are
--     engine.
-- This is the data-driven upgrade the bond README flagged: a world can ease
-- or tighten the ceiling by editing a WorldSetting (an oracle, or a
-- constitutional POLICY) without re-enacting this validator.
-- ===========================================================================

local DEFAULT_CAP = "0"   -- "0" forbids issuance entirely; raise to permit some.

if ctx.op.type ~= "issue_money" then
  return true
end

-- A governed override (a {"cap": "<money-str>"} signal) wins; the in-source
-- default is the constitutional floor when no signal is posted.
local setting = ctx.query.world_setting("monetary:issue_cap")
local cap = (setting ~= nil and setting.cap) or DEFAULT_CAP

if tonumber(ctx.op.amount) > tonumber(cap) then
  return {allow = false,
          reason = "monetization cap breached: issue " .. ctx.op.amount .. " > " .. cap}
end
return true
