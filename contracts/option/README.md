# Options — Step 5d reference contract (the asymmetric right)

An **exchange** (a CCP) matches a **buyer** (the holder of a right) and a
**writer** (the obligated party). The buyer pays a one-time **premium** to the
writer (the price of the right) and posts *no* margin; the writer posts
**margin** (collateral for the obligation). At settlement the exchange pays the
buyer the **intrinsic value** **only if the option is in the money** —
otherwise the writer's margin returns whole (the premium has already settled
hands). The deficiency case (payout exceeds margin) reuses the futures
``seize``→``to_entity`` pattern.

> Option — same shape, settlement pays the long only if in the money.
> — `docs/actors.md`, Step 5d

## The headline asymmetry vs futures

| | future | option |
|---|---|---|
| **buyer's position** | obligated | has a **right** (no obligation) |
| **seller's position** | obligated | has an **obligation** |
| **what the buyer posts** | margin | **premium** (the price of the right) |
| **what the seller posts** | margin | **margin** (collateral) |
| **settlement pays the long** | always (both sides paid) | **only if in the money** |
| **P&L** | symmetric (zero-sum) | asymmetric (premium + intrinsic) |

A future is a *symmetric* pair; an option is *asymmetric*. The premium — not
margin — is what the buyer pays; the margin — posted by the writer alone — is
what guarantees the promise. At settlement a future pays both sides their
credit; an option pays the buyer only if the intrinsic value is positive,
otherwise the writer's margin returns untouched. That single difference —
*pays only if in the money* — is the entire economic content of "a right, not
an obligation."

## The model

| | concept |
|---|---|
| **exchange** | a BUSINESS (the CCP). Holds the margin pool. Needs `SEIZE` for the deficiency case. |
| **buyer** | the holder of the right. Pays a one-time premium; posts no margin. |
| **writer** | the obligated party. Collects the premium; posts margin. |
| **kind** | `call` (right to buy at strike) or `put` (right to sell at strike). |
| **strike** | the exercise price. |
| **premium** | the price of the right — paid buyer → writer at origination, never returned. |
| **margin** | the writer's collateral — held in the pool, returned (minus payout) at settlement. |
| **intrinsic value** | call: `max(0, signal − strike) × qty`; put: `max(0, strike − signal) × qty`. |

## Files

| file | role |
|------|------|
| `option.py` | the book (data): `Exchange`, `open_exchange`/`open_option` + `settle` + read helpers. Writes the deficiency oracle. |
| `option.lua` | the BEHAVIOUR mark-to-market script: reads the signal, stamps intrinsic value + writer credit, flags breach/expiry. |
| `option_sufficiency.lua` | VALIDATOR — gates the exchange's `seize` to a documented deficiency (fail-closed). |

## `state` shape (exchange's BEHAVIOUR script)

```jsonc
{
  "currency": "USD",
  "maintenance_fraction": "0.5",
  "next_pos_id": 2,
  "positions": {
    "1": {
      "kind":           "call",          // call | put
      "buyer":          "<eid>",         // holder of the right
      "writer":         "<eid>",         // the obligated party
      "symbol":         "GRAIN",
      "quantity":       "100",
      "strike":         "5.00",          // exercise price
      "premium":        "50",            // paid buyer -> writer at open
      "margin":         "200",           // writer's posted collateral
      "expiry":         10,              // absolute tick
      "buyer_value":    "100.0000",      // intrinsic value (stamped by option.lua)
      "writer_credit":  "100.0000",      // margin - buyer_value (stamped by option.lua)
      "last_mark":      2,
      "status":         "open"           // open | breached | expired | settled
    }
  },
  "total_open_interest": "100.0000"
}
```

## The mark-to-market cycle (`option.lua`)

Each tick the BEHAVIOUR script reads the signal and stamps:

- **`buyer_value`** = the intrinsic value (what the option is worth if exercised now).
- **`writer_credit`** = `margin − buyer_value` (what the writer would get back if settled now).
- **breach** if `writer_credit < margin × maintenance` (the writer is under-collateralized).
- **expiry** if `ctx.tick ≥ expiry`.

Mark-to-market is a **pure book update**: no money moves (the margin pool is
the writer's posted cash; the values are just numbers in state), exactly like
the bank's intra-bank `pay` and the futures' credit update. The signal comes
from the **same oracle a future reads** (`futures:price:SYMBOL`) — the
underlying's price is shared infrastructure; a future and an option on GRAIN
read the same number.

## Settlement (`settle()`) — three cases

| case | condition | outcome |
|---|---|---|
| **out of the money** | intrinsic = 0 | buyer gets nothing; writer's margin returns whole |
| **in the money, collateralized** | 0 < intrinsic ≤ margin | buyer gets intrinsic; writer gets remainder |
| **deficiency** | intrinsic > margin | buyer gets full pool + seized goods from writer |

The deficiency case reuses the **exact futures pattern**: the exchange `seize`s
goods worth the deficiency from the writer and redirects them to the buyer
(`to_entity`), making the buyer whole without cash-conversion. If the writer
holds no goods (or the exchange lacks `SEIZE`, or a validator vetoes), the
buyer takes a haircut. Note the buyer is *never* seized from — the buyer has no
obligation. This is simpler than futures' settle (which branches on
long-vs-short deficiency); here the writer is always the party at risk.

`settle()` is Python (not Lua) for the same reason as futures: the deficiency
case needs try/except branching (seize, and if no goods, haircut) that deferred
Lua-intent resolution cannot express.

## The option-sufficiency validator + the deficiency oracle

Structurally identical to futures' `margin_sufficiency.lua` — only the oracle
prefix differs. A VALIDATOR cannot read the exchange's script state, so the
deficiency is mirrored into a WorldSetting
(`option:deficiency:<WRITER>:<SYMBOL>`), written by `settle()` before seizing.
No oracle → veto (undocumented seizure, fail-closed); quantity over max → veto.
This is the same 5c oracle pattern as the loan's usury cap, the futures'
deficiency, and the insurance coverage cap.

## Contrast: the six reference contracts

| contract | what it is | the primitive it validates |
|---|---|---|
| **bond** | a claim (debt) | `ctx.tick` (scheduled servicing) |
| **bank** | a money-creator (deposit money) | two-tier money (shadow ledger) |
| **loan** | a debt-with-enforcement | `levy` + `seize` (foreclosure) |
| **futures** | symmetric margin | `seize` as a margin call + the signal |
| **insurance** | an event trigger | `ctx.events` (delivered outcome) |
| **option** | an asymmetric right | settlement *pays only if in the money* |

Together: a claim, a money-creator, a debt-with-enforcement, a symmetric
margin, an event-trigger, and an asymmetric right. That is the Step 5d
reference library — six contracts, each exercising a distinct engine affordance.

## Usage

```python
from contracts.option.option import open_exchange, open_option, settle

exchange = open_exchange(session, "Desk", "USD")
exchange.entity.capabilities = [capabilities.SEIZE]   # clearinghouse license

# A call: the right to buy 100 GRAIN at 5.00, expiring tick 10.
# Buyer pays 50 premium; writer posts 200 margin.
open_option(session, exchange, buyer, writer, "call", "GRAIN",
            Decimal("100"), Decimal("5.00"), expiry=10,
            premium=Decimal("50"), margin=Decimal("200"))

# ...the signal moves. At expiry (or early as a margin call):
summary = settle(session, exchange, 1)
# In the money -> buyer gets the intrinsic value.
# Out of the money -> writer's margin returns; buyer keeps the loss (the premium).
```

## Limitations & extensions

- **European exercise** (settle at expiry or early as a forced close-out).
  American exercise (early exercise at the buyer's discretion) is an extension:
  add a `ctx.action` for the buyer to request exercise, and have `option.lua`
  honor it.
- **Cash-settled** (no physical delivery). Physical delivery (the buyer pays
  the strike and receives the good) is an extension of `settle()`.
- **One writer per position** (no writer syndicate). A syndicated write (multiple
  writers sharing the obligation) is an extension of the position book.
- **The premium is paid once at origination.** Recurring premiums (an annuity
  structure) are a POLICY-script extension.
- **Single underlying per position.** Spread/barrier/exotic options are further
  extensions of the intrinsic-value computation in `option.lua`.

## When this stops being enough

This contract covers vanilla European call/put options cleared by a CCP. It
stops being enough when options are **traded** on a secondary market (the buyer
sells the right to a third party — Fork B territory), or when the payoff is
**path-dependent** (Asian, lookback, barrier — the intrinsic value depends on
the price history, not just the spot). Until then: a premium, a margin, a
strike, and a signal — and *pays only if in the money*. That is enough to close
the Step 5d library.
