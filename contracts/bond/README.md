# Government bond — Step 5d reference contract (Fork A)

A **government bond** as *data + a Lua script* — not an engine feature. It is
the first deliverable of the reference contract library and the empirical test
of the substrate decisions beneath it.

> A financial instrument is data + a behaviour/policy script that interprets
> that data each tick. A bond is no more an engine feature than a tax schedule
> is — a dedicated `Contract` table would be the same mistake a `Tax` table
> would be. — `docs/actors.md`, Step 5 design

## The model

| concern       | mechanism                                                        |
| ------------- | ---------------------------------------------------------------- |
| the claim     | a `Holding` row whose symbol is the bond's (Step 5b, **Fork A**) |
| who owns what | `ctx.query.holders(symbol)` — the **live** cap table             |
| the terms     | `state.bonds[SYMBOL]` on the issuer's servicing script           |
| the clock     | `ctx.tick` (Step 5a) — not a run-counter                         |
| coupons/face  | `transfer` from the issuer to each holder                        |
| the sale      | `transfer` buyer→issuer + `adjust_holding` buyer+units           |

Because the claim *is* a holding, **a bond trades for free**: a secondary-market
sale is an `adjust_holding` (and a payment), and the next coupon pays whoever
holds the bond then. No register cached in script state — that would go stale
the moment a bond changes hands. `test_transferred_bond_pays_new_holder` is the
test Fork A exists to pass.

Because the sale is a `transfer` of *existing* money, **a bond sale creates no
money** — only `issue_money` (a `MONETARY_AUTHORITY` op) does. The lifecycle is
money-conserving end to end; `test_bond_sale_does_not_create_money` checks the
total supply is invariant across issuance, coupons, and redemption.

## Files

| file                    | role                                                    |
| ----------------------- | ------------------------------------------------------- |
| `gov_bond.lua`          | the POLICY servicing script (the *policy*). Bind to the issuer. |
| `bond.py`               | `BondTerms`, `issue_bond`, `redeem_holdings` (the *data*). |
| `monetization_cap.lua`  | optional VALIDATOR — a constitutional cap on money creation. |

## `state` shape

`issue_bond` writes one entry per bond into the servicing script's `state.bonds`:

```jsonc
{
  "GOVBOND-T4": {
    "currency": "USD",     // paying currency (an issuer account must exist)
    "face": "100",         // redemption value per unit (money string)
    "coupon": "2.5",       // coupon per unit per period (money string)
    "interval": 4,         // ticks per coupon period
    "issue_tick": 0,       // tick the bond was issued at
    "periods": 2,          // total coupon periods
    "maturity": 8,         // redemption tick (== issue_tick + periods*interval)
    "coupons_paid": 0,     // periods settled (advanced by the script)
    "redeemed": false      // set true once face is paid
  }
}
```

The servicing script is **generic**: it iterates `state.bonds` and honours
whatever is there. Issuing a second bond under the same script just adds another
entry.

## Usage

```python
from contracts.bond.bond import BondTerms, issue_bond, redeem_holdings

terms = BondTerms(
    symbol="GOVBOND-T4",
    face=Decimal("100"), coupon_rate=Decimal("0.05"),  # 5% of face per period
    interval=2, periods=2, currency="USD",             # matures at tick 4
)
issue_bond(session, gov, terms, [(alice, 10)])   # sells 10 units to alice at par
# ... run ticks; the bound POLICY script pays coupons then face ...
redeem_holdings(session, "GOVBOND-T4")           # extinguish the units
```

`issue_bond` installs the servicing script (idempotent), collects the proceeds
(a `transfer`), credits the buyers (an `adjust_holding`), and registers the
terms. `issue_tick` defaults to `0`; pass the latest committed tick to anchor a
bond issued mid-simulation.

## The money/goods split

Redemption is two halves, split exactly as issuance is:

1. **Money** — the servicing script pays the face to each holder (`transfer`).
   This is driven by `ctx.tick` and fires automatically at maturity.
2. **Goods** — the bond units are retired by `redeem_holdings`
   (`adjust_holding` the holding back to zero).

The split is forced by the engine's boundary: **scripts move money, not goods**.
There is no Lua action that adjusts a holding, by design — goods movement is an
ownership/admin boundary. So a redeemed bond's units linger harmlessly (a
zero-quantity holding is invisible to `holders`, which filters `quantity > 0`)
until the issuer's operator runs `redeem_holdings`.

## Schedule robustness (`ctx.tick`)

Coupons are computed from whole periods elapsed since issue:
`floor((ctx.tick - issue_tick) / interval)`, capped at `periods`. Because this
reads the **wall tick**, a compute-budget skip does not drift anything — the next
run simply catches up (paying accrued coupons in one go). `coupons_paid` records
how many periods have been settled so each is paid exactly once.

## Limitations & extensions

- **Arrears.** A coupon is marked settled when the transfer is *queued*, not
  when it settles. If the issuer is insolvent the transfer is rejected (visible
  in `ctx.events`) but the period still counts as paid — the coupon is not
  automatically carried forward. A production bond would read the rejection
  events and decrement `coupons_paid`; the reference keeps the happy path
  honest and simple. `test_missed_coupon_when_issuer_insolvent` documents this.
- **Goods retirement is manual.** See the money/goods split above.
- **Whole units only.** Quantities are floored to integers (you hold a number of
  bonds). Fractional bond holdings would need the precision work a loan's
  accruing balance will demand.
- **No per-holder coupon currency.** Coupons pay to the holder's first account
  in *their* currency; cross-currency holders are out of scope here.

## The constitutional cap (`monetization_cap.lua`)

A bond world may want to bind its issuer at the constitutional tier: forbid
money creation outright so bonds can be serviced **only from existing funds**,
never by monetising the debt. Install `monetization_cap.lua` as a VALIDATOR — at
`CAP = "0"` it is a hard-money standard. Raising `CAP` is a constitutional act
(re-enact via `set_validator` at supermajority). This is the same
validator-constrains-policy pattern `fiscal_policy` already uses, aimed at the
two-tier-money boundary the bond demonstrates. (A data-driven `CAP` keyed off a
`WorldSetting` is the natural Step-5c upgrade once `ctx.query.world_setting`
ships.)

## When this stops being enough (Fork B)

Fork A covers every **fungible, identical-terms** claim. It stops being enough
the day a *non-fungible, traded* claim appears — a securitised loan with *its*
collateral, a bespoke OTC option with *its* strike. That is the trigger to
re-open Step 5b as Fork B (a first-class `Position`/`Instrument` model). Until
then: a bond is a holding, and that is enough.
