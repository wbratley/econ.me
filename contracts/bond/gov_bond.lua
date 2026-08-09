-- contracts/bond/gov_bond.lua
-- ===========================================================================
-- Government bond — servicing engine (Step 5d reference contract, Fork A).
-- ===========================================================================
-- A bond here is DATA + this POLICY script (design.md §2: a bond is no more
-- an engine feature than a tax schedule). Bind this script to the ISSUER
-- (a government). Each tick it honours every bond registered in its state:
--   * coupons on schedule,
--   * face at maturity,
-- and then marks the bond redeemed.
--
-- The ownership primitive is Fork A: a bond unit *is* a Holding row whose
-- symbol is the bond's. The live register of who owns how much is therefore
-- ctx.query.holders(symbol) -- never a cap table cached in state, which goes
-- stale the moment a bond trades. The clock is ctx.tick (Step 5a), NOT a
-- run-counter, so a compute-budget skip does not desynchronise maturities.
--
-- This script moves MONEY only (coupons, face). It cannot retire the bond
-- units themselves -- there is no Lua action that adjusts a holding, by
-- design (goods movement is an admin/ownership boundary). A redeemed bond's
-- units linger harmlessly until the issuer's operator runs the
-- redeem_holdings() helper to extinguish them. See README.md.
--
-- state shape (written by contracts.bond.issue_bond; read/marked here):
--   state.bonds[SYMBOL] = {
--     currency     = "USD",   -- paying currency (must be an issuer account)
--     face         = "100",   -- redemption value per unit (money string)
--     coupon       = "2.5",   -- coupon per unit per period (money string)
--     interval     = 4,       -- ticks per coupon period
--     issue_tick   = 0,       -- the tick the bond was issued at
--     periods      = 2,       -- total coupon periods
--     maturity     = 8,       -- redemption tick (= issue_tick + periods*interval)
--     coupons_paid = 0,       -- periods settled so far (advanced below)
--     redeemed     = false,   -- set true once face is paid
--   }
-- ===========================================================================

-- Account precision is Numeric(18,4); %.4f absorbs float dust and yields a
-- string Decimal() parses exactly. (2.5 * 2 * 100 -> "500.0000".)
local function money(x)
  return string.format("%.4f", x)
end

-- The issuer's settlement account in the bond's currency.
local function paying_account(currency)
  for _, a in ipairs(ctx.accounts) do
    if a.currency == currency then return a.id end
  end
  return nil
end

for symbol, b in pairs(ctx.state.bonds or {}) do
  if not b.redeemed and b.interval and b.interval > 0 then
    local acct = paying_account(b.currency)
    if acct then
      -- Whole coupon periods elapsed since issue, capped at the bond's
      -- total period count. floor() keeps it integer-exact; ctx.tick is the
      -- wall clock, so a skipped tick simply catches up later.
      local elapsed = math.min(
        math.floor((ctx.tick - b.issue_tick) / b.interval), b.periods)
      local due = elapsed - b.coupons_paid
      local holders = ctx.query.holders(symbol)

      if due > 0 then
        local per_unit = tonumber(b.coupon) * due
        for _, h in ipairs(holders) do
          local qty = math.floor(tonumber(h.quantity) or 0)
          if qty > 0 and h.account_id then
            ctx.action.transfer(acct, h.account_id, money(per_unit * qty),
              "coupon:" .. symbol .. ":t" .. ctx.tick)
          end
        end
        b.coupons_paid = elapsed   -- settled at queue time; see README (arrears)
      end

      -- Redemption: pay the face. (Bond units are retired separately.)
      if ctx.tick >= b.maturity then
        local face = tonumber(b.face)
        for _, h in ipairs(holders) do
          local qty = math.floor(tonumber(h.quantity) or 0)
          if qty > 0 and h.account_id then
            ctx.action.transfer(acct, h.account_id, money(face * qty),
              "redeem:" .. symbol .. ":t" .. ctx.tick)
          end
        end
        b.redeemed = true
      end
    end
  end
end
