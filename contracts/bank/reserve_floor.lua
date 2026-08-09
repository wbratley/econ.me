-- contracts/bank/reserve_floor.lua
-- ===========================================================================
-- Constitutional liquidity rule for a bank (Step 5d reference VALIDATOR).
-- ===========================================================================
-- Bind this as a VALIDATOR on the BANK entity. It fires only for the bank's
-- OWN outbound transfers (withdrawals / interbank outflows) -- because a
-- transfer's op.entity_id is the sender's, and _applicable_scripts keeps an
-- entity-bound validator to its entity. A deposit (customer -> bank) sends
-- as the customer, so this never gates cashing IN, only draining OUT.
--
-- It enforces a reserve floor: a withdrawal that would push reserves below
-- the floor is denied. At floor 0 it is a pure solvency guard (the engine's
-- own InsufficientFundsError already prevents overdraft); a positive floor
-- is a self-imposed liquidity rule / capital control -- the discipline a
-- bank's charter (or a regulator) may want bound at the constitutional tier.
--
-- WHY A FLOOR, AND NOT A CAP ON LENDING. The bank creates money by lending,
-- but lending is a BOOK ENTRY in script state, not an engine operation --
-- there is no `lend` intent for a VALIDATOR to gate. The engine cannot
-- constitutionally constrain a book entry, because a book entry is not an
-- engine op. A reserve REQUIREMENT (cap lending at a ratio of deposits) must
-- live in the bank's own script or a platform audit, not here. What IS an
-- engine op is a `transfer` of base money out of reserves -- and that is
-- what this floor gates. That asymmetry is itself the lesson: credit money
-- lives outside the engine's enforcement surface by construction.
--
-- The floor is two-layered (the decision-rule/data-effect split):
--   * a DEFAULT_FLOOR in source -- changing it is a constitutional act
--     (re-enact via set_validator at supermajority);
--   * a governed override read live each op from the "bank:reserve_floor"
--     WorldSetting ({"floor": "<money>"}) via ctx.query.world_setting (5c).
-- ===========================================================================

local DEFAULT_FLOOR = "0"   -- "0" = guard only against overdraft.

if ctx.op.type ~= "transfer" then
  return true
end

local setting = ctx.query.world_setting("bank:reserve_floor")
local floor = (setting ~= nil and setting.floor) or DEFAULT_FLOOR

local current = tonumber(ctx.query.balance(ctx.op.from_account_id))
local after = current - tonumber(ctx.op.amount)
if after < tonumber(floor) then
  return {allow = false,
          reason = "reserve floor breached: " .. string.format("%.4f", after)
                   .. " < " .. floor}
end
return true
