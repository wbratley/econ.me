-- contracts/option/option_sufficiency.lua
-- ===========================================================================
-- Option-sufficiency check -- constitutional constraint on forced liquidation
-- (Step 5d).
-- ===========================================================================
-- Bind this as a VALIDATOR on the EXCHANGE entity. It fires only for the
-- exchange's OWN seizures (a seize's op.entity_id is the seizing authority's).
-- It enforces the rule that a deficiency seizure must be backed by a
-- DOCUMENTED DEFICIENCY, and must not exceed it: the exchange may not
-- expropriate the writer's goods without proof that the writer's margin was
-- insufficient to cover the buyer's claim, nor take more than that
-- insufficiency warrants. This is the option analogue of futures'
-- margin-sufficiency check -- a constitutional backstop that makes a rogue
-- exchange's naked seizure fail-closed.
--
-- THE ORACLE. A VALIDATOR has only its OWN state + queries -- it cannot read
-- the exchange's BEHAVIOUR script state where the position book lives. So the
-- deficiency is mirrored into a queryable WorldSetting (the 5c signal pattern,
-- exactly as the futures deficiency and the loan's usury cap), keyed by the
-- writer and symbol: option:deficiency:<EID>:<SYMBOL> = {max = "<qty>"}.
-- settle() writes it immediately before seizing; this validator reads it. No
-- oracle row -> the seizure is undocumented -> veto. A quantity over the
-- documented max -> veto.
--
-- Structurally identical to futures/margin_sufficiency.lua -- only the oracle
-- prefix differs. The seize primitive is the same; the enforcement role
-- (writer's margin call) is the option's.
-- ===========================================================================

if ctx.op.type ~= "seize" then
  return true
end

local oracle = ctx.query.world_setting(
  "option:deficiency:" .. ctx.op.from_entity_id .. ":" .. (ctx.op.symbol or ""))
if oracle == nil then
  return {allow = false,
          reason = "option deficiency seizure without a documented deficiency for "
                   .. ctx.op.from_entity_id}
end

local seizing = tonumber(ctx.op.quantity)
local max = tonumber(oracle.max or "0")
-- A sliver of tolerance for the 4dp quantisation in settle().
if seizing > max + 0.0001 then
  return {allow = false,
          reason = "seize " .. string.format("%.4f", seizing)
                   .. " exceeds documented option deficiency "
                   .. string.format("%.4f", max)}
end
return true
