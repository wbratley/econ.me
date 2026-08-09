# Secured loan — Step 5d reference contract (the enforcement spine of private debt)

A **secured loan** as *data + a Lua script* — the reference contract that
validates `levy` and `seize` as the enforcement spine of private debt. A lender
lends real base money, takes collateral, and on default **compels** recovery:
cash by `levy`, goods by `seize`. These are the same privileged primitives the
state uses to tax and expropriate — here *delegated* to a private creditor.

> A lender's script holds the loan book, accrues interest, levies payment or
> seizes collateral on default. — `docs/actors.md`, Step 5d

## The model (vs. the bank)

| | **bank** (`contracts/bank`) | **loan** (this contract) |
|---|---|---|
| lends | deposit money (a **book entry** — created) | base money (a **`transfer`** — existing) |
| secured? | no — unsecured book money | **yes** — collateral pledged |
| enforcement on default | none — a **bank run** (`InsufficientFundsError`) | **`levy` + `seize`** — foreclosure |
| creates money? | **yes** (the loan creates a deposit) | no (it moves existing money) |

The bank's failure mode is a bank run — it owes more deposit money than it
holds base money, and a withdrawer is simply refused. The loan's failure mode
is a **foreclosure** — the lender *compels* recovery by force of the engine's
privilege. That difference is the whole point: this contract exists to exercise
`levy`/`seize` on private debt.

## The enforcement spine

A loan that goes unpaid past maturity is foreclosed by `enforce()` in two steps:

1. **`levy`** — take cash by force from the borrower's settlement account, up to
   the debt. The lender must hold the `LEVY` capability; the usury-cap
   VALIDATOR gates the amount. A usurious loan's levy is vetoed; the lender
   falls through to seizure.
2. **`seize`** — take the pledged collateral by force. The lender must hold the
   `SEIZE` capability. If the collateral has fled (the borrower sold it — the
   engine has no lien), `seize` raises `InsufficientHoldingsError`: caught and
   reported, not crashed.

Both primitives are **money-/goods-conserving** — `levy` is a DEBIT/CREDIT pair
(like `transfer`); `seize` moves a holding quantity. Neither creates anything.
The *compulsion* is carried by op-type and `rule_ref`, not by a new transaction
flavour.

### Non-recourse

Seizing the collateral **settles** the loan regardless of recovery. The lender
forecloses, takes what it can, the debt is extinguished — the lender bore the
collateral risk. (A recourse model would keep the deficiency alive; that is a
policy choice layered on `enforce()`, not an engine concern.)

## The lender's license (capability delegation)

The lender is **not** born with enforcement power. `LEVY` and `SEIZE` are
sovereign capabilities — the state grants them separately (a lender's license,
a court judgment). Without them `enforce()` raises `MissingCapabilityError`: a
creditor without the state's backing cannot collect by force. This is the same
delegation the state itself uses (tax = `levy`; confiscation = `seize`), now
lent to a private party to collect a private debt.

## Files

| file | role |
|------|------|
| `loan.py` | the book (data): `Loan`, `open_lender`, `create_loan`/`repay`/`enforce` + read helpers. Also maintains the loan **oracle** (below). |
| `loan.lua` | the BEHAVIOUR servicing script (policy): accrues skip-safe interest each tick from `ctx.tick`; marks default at maturity; stamps the at-risk book. |
| `usury_cap.lua` | VALIDATOR — a statutory ceiling on collectible interest, gating the lender's `levy`. |

## `state` shape (lender's BEHAVIOUR script)

```jsonc
{
  "currency": "USD",
  "default_rate": "0.01",
  "loans": {
    "<borrower_id>": {
      "account_id":   "<settlement account>",   // what gets levied
      "principal":    "100",
      "rate":         "0.02",                   // per-tick interest fraction
      "interest_due": "4.0000",                 // accrued by loan.lua
      "issue_tick":   0,
      "maturity":     5,                        // absolute due tick
      "last_accrued_tick": 2,                   // advanced by loan.lua
      "paid":         "0",                      // principal+interest repaid
      "collateral":   {"symbol": "GRAIN", "quantity": "50"},  // or null
      "status":       "active",                 // active | default | settled
      "default_tick": null                      // set by loan.lua
    }
  },
  "total_outstanding": "104.0000"               // stamped by loan.lua
}
```

`loan.py` owns `principal`/`paid`/`collateral`/`status`; `loan.lua` owns
`interest_due`/`last_accrued_tick`/`default_tick`/`total_outstanding`. The two
halves coordinate through `script.state`, exactly as the bank's `bank.py` and
`bank.lua` do.

## The loan oracle (why the usury cap reads a WorldSetting)

A VALIDATOR has only **its own state + queries** — it cannot read the lender's
BEHAVIOUR script state, where the loan book lives. So for a usury-cap validator
to see a loan's terms, those terms must live where a query can reach them. The
Python helpers mirror each loan's cap-relevant terms (`principal`, `rate`,
`issue_tick`, `paid`) into a queryable WorldSetting, `loan:account:<acct_id>`,
which the validator reads via `ctx.query.world_setting` (Step 5c).

This is the same **decision-rule / data-effect split** as fiscal policy: the
loan is *data* (here, an oracle row); the `levy` is the *mechanism* the cap
gates. A bonus: a loan's terms become observable without reading script
internals.

### Why a rate cap is enforced at collection, not origination

The engine cannot stop two parties from **writing** a 50%-per-tick loan — a
loan is a `transfer` plus a book entry, neither of which fires a validator. It
can only stop the lender from using the state's `levy` power to **collect**
usurious interest. The cap bites at the enforcement boundary — the one place
the engine's privilege touches a private debt. (A voluntary repayment at any
rate is the borrower's own money moving of its own accord; the engine has no
grounds to stop it. Only compelled collection — `levy` — is the state's act.)

The cap is **two-layered**: a `DEFAULT_CAP` (5%/tick) in source — changing it
is a constitutional act — and a governed override `loan:usury_cap`
(`{"rate": "<fraction>"}`) read live each op.

## Usage

```python
from contracts.loan.loan import open_lender, create_loan, enforce, loan_due
from econengine import capabilities

lender = open_lender(session, "Shylock", "USD", capital=Decimal("100000"))
lender.entity.capabilities = [capabilities.LEVY, capabilities.SEIZE]  # license

create_loan(session, lender, borrower, Decimal("500"),
            rate=Decimal("0.02"), term=5,
            collateral={"symbol": "GRAIN", "quantity": "50"})
# ...ticks pass, interest accrues, maturity arrives unpaid...
summary = enforce(session, lender, borrower)   # levy cash, seize collateral
```

## Limitations & extensions

- **No lien.** Collateral stays with the borrower until foreclosure (a
  mortgage, not a pawn). A borrower who sells the pledged goods before default
  leaves the lender nothing to seize; `seize` raises `InsufficientHoldingsError`
  — the honest signal that un-liened collateral is a risk the lender bears. A
  lien/encumbrance model would be a Fork-B concern (a non-fungible claim on a
  specific good).
- **Simple interest, bullet at maturity.** Interest accrues on original
  principal; partial repayment does not reduce future interest. An amortising
  loan would reduce `principal` on each repayment.
- **Non-recourse.** Foreclosure settles the loan regardless of recovery. A
  recourse model (deficiency survives) is a policy change in `enforce()`.
- **One settlement account per borrower** (in the loan's currency).

## When this stops being enough

This contract covers a single bilateral secured loan. It stops being enough
when loans are **traded** (a secondary market in loan claims — that is Fork B,
a non-fungible claim), or when collateral is **fractional/pooled** (securitisation).
Until then: a loan is a transfer, a book entry, and a foreclosure — and that is
enough.
