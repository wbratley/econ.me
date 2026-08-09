# Insurance — Step 5d reference contract (`ctx.events` as a trigger source)

An **insurer** collects **premiums** into a **risk pool** and pays a **death
benefit** to a designated **beneficiary** when a **trigger event** fires for a
**policyholder**. The trigger is read from **`ctx.events`** — the previous
tick's outcomes — not polled from a signal (Step 5c's other face). This is the
contract that validates the one engine affordance no earlier contract
exercises.

> Insurance — premium `transfer` in, risk pool in `state`, payout on a trigger
> event read from `ctx.events`.
> — `docs/actors.md`, Step 5d

## Why a POLICY script, not BEHAVIOUR

A BEHAVIOUR script sees only **its own entity's** events; the insurer must
watch its **policyholders'** events (a death is an event on the deceased, not
the insurer). Only a **POLICY** script sees every entity's events — so the
insurer's trigger-and-pay engine is a POLICY script. The POLICY/BEHAVIOUR
distinction is exactly an event-visibility distinction: POLICY = global,
BEHAVIOUR = own-entity (see `tick.py`).

## The model

| | concept |
|---|---|
| **insurer** | a BUSINESS entity. Holds the risk pool. |
| **policyholder** | the insured. Pays a one-time premium at underwriting. |
| **beneficiary** | the recipient of the death benefit (a third party). |
| **trigger** | an event in `ctx.events` matching the policyholder. Default: `entity_incapacitated` (a death). |
| **coverage** | the death benefit amount. |
| **risk pool** | the commingled account: premiums in, payouts out. |

The default trigger — `entity_incapacitated` — fires when a policyholder crosses
an incapacitating condition threshold (`conditions.py`): starvation,
disease, etc. The engine emits the event; the insurer's POLICY script sees it
the next tick and pays. This is a *real* engine event, not a synthetic one.

## Files

| file | role |
|------|------|
| `insurance.py` | the book (data): `Insurer`, `open_insurer`/`underwrite` + read helpers. Publishes the coverage oracle. |
| `insurance.lua` | the POLICY trigger-and-pay script: scans `ctx.events`, marks triggers, pays claims each tick. |
| `coverage_cap.lua` | VALIDATOR — gates the insurer's outbound transfers to documented coverage (fail-closed). |

## `state` shape (insurer's POLICY script)

```jsonc
{
  "currency": "USD",
  "pool_account_id": "<acct>",
  "trigger": "entity_incapacitated",
  "policies": {
    "<policyholder_id>": {
      "beneficiary_account_id": "<acct>",
      "coverage":   "1000",
      "premium":    "50",
      "term":       20,            // absolute tick; null = perpetual
      "issued_tick": 1,
      "triggered":  false,         // owned by insurance.lua
      "trigger_tick": null,
      "paid":       false          // owned by insurance.lua
    }
  },
  "total_coverage": "1000.0000"     // stamped by insurance.lua
}
```

`insurance.py` owns the policy record; `insurance.lua` owns `triggered`/
`trigger_tick`/`paid`/`total_coverage`.

## The trigger-and-pay cycle (`insurance.lua`)

Each tick the POLICY script does the insurer's whole back office:

1. **Detect**: scan `ctx.events` for the trigger event type. For each event
   whose `entity_id` is a policyholder, mark that policy `triggered`.
2. **Pay**: for each triggered-but-unpaid policy, if the pool can cover it, queue
   a `ctx.action.transfer` from the pool to the beneficiary and mark `paid`.

### Why payout is Lua-driven (unlike futures' Python `settle`)

Futures' `settle` needs try/except branching (seize, and if the defaulter has no
goods, the winner takes a haircut) that deferred Lua-intent resolution cannot
express. Insurance payout has **no such branching**: a **local pool counter**
tracks the remaining balance, decrementing as it queues payouts, so the pool is
never over-committed; and the coverage oracle (written at underwriting) matches
the payout amount exactly, so the coverage-cap VALIDATOR cannot veto. With both
guards the queued transfer cannot fail — so marking `paid` before resolution is
safe. This is the cleanest possible demonstration of *event → action*.

### Risk-pool exhaustion is graceful

If two policyholders die the same tick and the pool covers only one, the local
counter pays one and defers the other (it stays `triggered`, `unpaid`). Next
tick, fresh premiums may fund the deferred payout — it retries automatically.
This is how a mutual insurer stays solvent under correlated claims: the pool is
the buffer.

## The coverage-cap validator + the coverage oracle

A VALIDATOR has only **its own state + queries** — it cannot read the insurer's
POLICY script state where the policy book lives. So the coverage is mirrored
into a queryable WorldSetting (`insurance:coverage:<ACCT>`), written by
`underwrite()` at policy creation — **the 5c oracle pattern**, exactly as the
loan's usury cap mirrors the loan book and the futures' deficiency mirrors the
margin book. The validator gates the insurer's outbound transfers: no oracle →
veto (undocumented payout, fail-closed); amount over the documented max → veto.
The risk pool is **locked to its payouts**.

This is the insurance analogue of the loan's usury cap and the futures'
margin-sufficiency check — the same constitutional-constraint pattern (a
mechanism gated by data it cannot itself read, mirrored into a queryable
oracle).

## Contrast: three trigger sources, now all exercised

| contract | trigger source | what drives the payout |
|---|---|---|
| **bond** | `ctx.tick` (scheduled) | a POLICY script pays coupons at intervals |
| **futures** | a signal (`world_setting`) | `settle()` reads the price oracle |
| **insurance** | **`ctx.events`** (an outcome) | a POLICY script scans last tick's events |

The engine now offers three ways a contract learns "something happened": the
clock (`ctx.tick`), a polled signal (`world_setting`), and a delivered event
(`ctx.events`). Insurance is the one that needed the third — and confirms it
works end-to-end against a real `entity_incapacitated` outcome.

## Usage

```python
from contracts.insurance.insurance import open_insurer, underwrite

insurer = open_insurer(session, "Mutual", "USD")
underwrite(session, insurer, policyholder, beneficiary,
           coverage=Decimal("1000"), premium=Decimal("50"))
# ...the policyholder crosses a condition threshold; conditions.py incapacitates
# them; the next tick the insurer's POLICY script sees entity_incapacitated in
# ctx.events and pays the beneficiary 1000 from the risk pool.
```

## Limitations & extensions

- **One-time premium** (collected at underwriting). Recurring premiums are a
  POLICY-script extension (collect per tick, lapse on non-payment).
- **One policy per beneficiary account** (the coverage oracle is keyed by
  beneficiary). Multiple policies to one beneficiary would need an aggregated
  oracle.
- **Single trigger type per insurer**, matched on `event.type` + `entity_id`.
  Richer matching (on symbol, quantity — e.g. crop-loss insurance keyed to
  `decay` of a specific good) is an extension of the Lua matching loop.
- **Fail-closed pool**: the coverage-cap validator locks the risk pool to
  documented payouts. The insurer's profit-taking (premium income beyond claims)
  is a separate governed op — out of scope for the reference contract.
- **The estate rule** sweeps an incapacitated entity's assets to an heir (or
  burns them). The insurance payout goes to the *designated beneficiary*, not
  the estate — the two are independent.

## When this stops being enough

This contract covers single-event, single-payout term life insurance cleared by
a mutual insurer. It stops being enough when policies are **traded** (a
secondary market in insurance claims — Fork B territory), or when the risk pool
is **reinsured** (a recursive insurer-of-insurers — just nest the contract).
Until then: a premium, a book entry, an event, and a payout — and that is
enough.
