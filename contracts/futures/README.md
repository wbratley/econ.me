# Futures + margin — Step 5d reference contract (seize as a margin call)

A **futures contract** as *data + a Lua script* — the reference contract that
validates `seize` as a **margin call** and validates the **signal convention**
(Step 5c). An **exchange** (a central counterparty, CCP) matches a **long**
(agrees to buy) and a **short** (agrees to sell) a quantity of a good at a
contract price, expiring at a tick. Both post **cash margin**. Each tick a Lua
script reads a **signal price** — an oracle `WorldSetting` — and **marks to
market**. On settlement the exchange pays out from the margin pool; if a side
is in **deficiency** (losses exceeded posted margin), the exchange `seize`s
goods from the defaulter and redirects them to the winner.

> An exchange CORPORATION matches longs/shorts, holds margin (`seize` on margin
> breach), settles cash or physical at expiry against a signal price (5c).
> — `docs/actors.md`, Step 5d

## The model

| | concept |
|---|---|
| **exchange** | a CCP (BUSINESS entity). Holds the commingled margin pool. Granted `SEIZE` by the state (a clearinghouse license). |
| **long / short** | the two sides. Both post initial margin (a `transfer` into the pool). |
| **signal** | the price oracle: `futures:price:<SYMBOL>` `WorldSetting`, posted by the platform between ticks. The engine reads it; it does not invent prices. |
| **credit** | a per-side mark-to-market book entry. Their sum is always the posted pool. |
| **deficiency** | a credit gone negative — losses exceeded posted margin. |

### Mark-to-market is a pure book update

The margin pool is commingled cash in the exchange's account; each side's
*credit* is just a number in script state. Mark-to-market recomputes the
credits each tick from the signal — **no money moves** (exactly like the bank's
intra-bank `pay`). The credits are zero-sum and cumulative from the contract
price, so the mark is inherently skip-safe (a dark feed or budget skip loses
nothing; the next lit tick catches up).

## Files

| file | role |
|------|------|
| `futures.py` | the book (data): `Exchange`, `open_exchange`/`open_future`/`settle` + read helpers. Maintains the deficiency oracle. |
| `futures.lua` | the BEHAVIOUR mark-to-market script (policy): reads the signal, recomputes credits, flags breach/expiry each tick. |
| `margin_sufficiency.lua` | VALIDATOR — gates the exchange's `seize` to a documented deficiency (fail-closed). |

## `state` shape (exchange's BEHAVIOUR script)

```jsonc
{
  "currency": "USD",
  "maintenance_fraction": "0.5",
  "next_pos_id": 2,
  "positions": {
    "1": {
      "long":          "<eid>",
      "short":         "<eid>",
      "symbol":        "GRAIN",
      "quantity":      "100",
      "price":         "5.00",            // contract price
      "expiry":        10,                // absolute tick
      "long_margin":   "100",             // initial margin (constant)
      "short_margin":  "100",
      "long_credit":   "110.0000",        // mark-to-market (stamped by futures.lua)
      "short_credit":  "90.0000",
      "last_mark":     2,
      "status":        "open"             // open | breached | expired | settled
    }
  },
  "total_open_interest": "100.0000"
}
```

`futures.py` owns `margins`/`status`; `futures.lua` owns `credits`/`last_mark`/
`total_open_interest`. The two halves coordinate through `script.state`, as the
bank and loan contracts do.

## Settlement — `seize` as a margin call

`settle()` reads the final signal and computes each side's credit. Two cases:

1. **Both solvent** (credits ≥ 0): the exchange pays each side their credit from
   the pool. The credits sum to exactly the pool (the P&L cancels), so this is
   money-conserving — no money created or destroyed.

2. **Deficiency** (one credit < 0): the winner takes the **entire cash pool**,
   *plus* the exchange `seize`s goods worth the deficiency from the defaulter
   (at the signal price) and **redirects them to the winner** (`to_entity`).
   This makes the winner whole **without any cash-conversion step** — the
   deficiency is settled *in goods*, directly. If the defaulter holds none (or
   the exchange lacks `SEIZE`, or a validator vetoes), the winner takes a
   haircut.

`settle()` may be called at expiry (`status == "expired"`) or early as a
**margin call** (`status == "breached"`) — the latter is the headline `seize`
use case. It is Python (not Lua) because the deficiency case needs try/except
branching (seize, and if the defaulter has no goods, haircut) that the engine's
deferred Lua-intent resolution cannot express — the same reason `loan.enforce()`
is Python.

### The exchange's license

The exchange is **not** born with seizure power. `SEIZE` is a sovereign
capability the state grants separately (a clearinghouse license). Without it,
`settle()` cannot seize a defaulter's goods and the winner takes a haircut —
the honest signal that a CCP without the state's backing cannot compel
liquidation.

## The margin-sufficiency validator + the deficiency oracle

A VALIDATOR has only **its own state + queries** — it cannot read the exchange's
BEHAVIOUR script state where the position book lives. So the deficiency is
mirrored into a queryable WorldSetting (`futures:deficiency:<EID>:<SYMBOL>`),
written by `settle()` immediately before seizing — **the 5c signal pattern
applied to the deficiency**, exactly as the loan's usury cap mirrors the loan
book. The validator reads it via `ctx.query.world_setting` and gates the
`seize`: no oracle → veto (undocumented seizure, fail-closed); quantity over
the documented max → veto. This is the roadmap's *"margin-sufficiency check on
futures"* — a constitutional backstop that makes a rogue exchange's naked
seizure fail-closed.

## Contrast: `seize` in three contracts

| contract | what `seize` takes | when | authority |
|---|---|---|---|
| **loan** | pledged **collateral** | foreclosure (default) | lender's `LEVY`+`SEIZE` license |
| **futures** | goods worth a **margin deficiency** | settlement / margin call | exchange's `SEIZE` license |
| (state) | anything | tax-in-kind, confiscation | the sovereign itself |

The same primitive — `seize` — is the enforcement spine in each. The futures
contract shows it serving *market discipline* (a margin call), not just debt
collection.

## Usage

```python
from contracts.futures.futures import open_exchange, open_future, settle
from econengine import capabilities

exchange = open_exchange(session, "Clearing", "USD")
exchange.entity.capabilities = [capabilities.SEIZE]   # clearinghouse license

pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                  quantity=Decimal("100"), price=Decimal("5.00"),
                  expiry=10, margin=Decimal("200"))["position"]
# ...the platform posts signal prices; futures.lua marks to market each tick...
summary = settle(session, exchange, pid)   # pays out; seizes defaulter's goods
```

## Limitations & extensions

- **Cash-settled only** (against the signal). Physical delivery (the short
  delivers the good, the long pays the contract price) is a policy variant of
  `settle()` — and would exercise `seize` to compel the short's delivery
  directly.
- **One symbol per position; deficiency seized in the underlying.** A
  multi-asset margin pool (cross-margining) would seize the cheapest-to-deliver
  good — a policy choice.
- **Deficiency oracle is per-entity-per-symbol.** A party with multiple
  positions in the same symbol aggregates; for a reference contract one at a
  time is enough.
- **Haircut on uncollateralised default.** If the defaulter has no goods (or
  the exchange lacks `SEIZE`), the winner takes a haircut — the pool only. A
  default waterfall (the exchange's capital, then insurance) is a layered
  extension.

## When this stops being enough

This contract covers a single bilateral margined future cleared by a CCP. It
stops being enough when positions are **traded** before expiry (a secondary
market in position claims — Fork B, a non-fungible claim), or when margin is
**cross-margined** across a portfolio. Until then: a future is two margins, a
book entry, a signal, and a seizure — and that is enough.
