# Commercial bank — Step 5d reference contract (two-tier money)

A **commercial bank** as *data + a Lua script* — not an engine feature. It is
the reference contract that validates the engine's most consequential claim:
**the engine is already a faithful two-tier monetary system**, and a bank
creates money by keeping a book, not by calling an engine primitive.

> A commercial bank's deposit balances are, by construction, a shadow ledger
> in the bank's script state — claims on the bank, created by lending, not
> base money. That is exactly how real banks create money, and it costs the
> engine nothing: 5d's bank reference script does not create money, it keeps
> a book. — `docs/actors.md`, Step 5 design

## The model (the whole point)

There are **two layers of money**, and they live in different places:

| concern          | where it lives                                          |
| ---------------- | ------------------------------------------------------- |
| **base money**   | a real engine `Account` (the bank's *reserve account*). Only `issue_money` (a `MONETARY_AUTHORITY` op) creates it. The bank never calls it. |
| **deposit money**| a *shadow ledger* in the bank's script `state.deposits[customer_id]`. A deposit is a **claim on the bank**, not base money. A customer's "checking balance" is this number. |

**Lending creates money.** `lend()` credits a deposit (a book entry in `state`)
and books a loan. It moves **no base money** and calls **no engine primitive**.
After a loan, total deposits *exceed* reserves — the bank has created money by
writing a number. `test_lending_creates_more_deposit_money_than_reserves` is
the test this contract exists to pass: after a loan, deposits > reserves, the
reserve ratio drops below 1, the base-money supply is **invariant**, and **not
one `ISSUANCE` transaction exists**. Credit money is a book, not a ledger
feature.

## Operations

Each is the honest mechanic, traceable end to end:

| op             | base money moves? | book entry                  | money created? |
| -------------- | ----------------- | --------------------------- | -------------- |
| `deposit`      | yes (cash → reserves)  | credit deposit         | no — conserves |
| `withdraw`     | yes (reserves → cash)  | debit deposit          | no — conserves |
| `pay` (same bank) | **no** — pure book | debit one, credit another | no — total deposits unchanged |
| `lend`         | **no**            | credit deposit + book loan  | **YES** — deposit money created |
| `repay`        | no                | debit deposit + reduce loan | destroys deposit money |
| `interbank_pay`| yes (reserves → reserves) | debit + credit books | no — conserves |

The contrast is the lesson: **`deposit`/`withdraw`/`interbank_pay` move base
money (`transfer`); `pay` and `lend` do not.** `pay` clears through the book;
`lend` creates from the book.

## Files

| file                 | role                                                       |
| -------------------- | ---------------------------------------------------------- |
| `bank.py`            | the book: `Bank`, `open_bank`, `deposit`/`withdraw`/`pay`/`lend`/`repay`/`interbank_pay` + read helpers (the *data*). |
| `bank.lua`           | the BEHAVIOUR servicing script (the *policy*): accrues loan interest each tick and reconciles the books. Bind to the bank. |
| `reserve_floor.lua`  | optional VALIDATOR — a liquidity rule on outbound transfers. |

## `state` shape

```jsonc
{
  "currency": "USD",
  "default_rate": "0.01",            // per-tick interest on loans
  "deposits": {                      // the SHADOW LEDGER (deposit money)
    "<customer_id>": "<amount>", ...
  },
  "loans": {
    "<borrower_id>": {
      "principal": "300",            // original principal
      "rate": "0.01",                // per-tick interest fraction
      "interest_due": "6.0000",      // accrued interest (OWNED by bank.lua)
      "last_accrued_tick": 2,        // advanced by bank.lua
      "paid": "0",                   // principal+interest repaid
      "repaid": false                // fully settled?
    }
  },
  "reserves": "100.0000",            // stamped by bank.lua (observable)
  "total_deposits": "400.0000",      // stamped by bank.lua (observable)
  "reserve_ratio": "0.2500"          // stamped by bank.lua (observable)
}
```

The Python helpers own `deposits` and the loan `principal`/`paid`/`repaid`;
`bank.lua` owns `interest_due` / `last_accrued_tick` (accrual) and the stamped
reconciliation fields. The two halves coordinate through `script.state`, exactly
as the bond's `issue_bond` and `gov_bond.lua` do.

## Usage

```python
from contracts.bank.bank import open_bank, deposit, lend, repay
from contracts.bank.bank import total_deposits, total_reserves, reserve_ratio

bank = open_bank(session, "FirstBank", "USD")
deposit(session, bank, alice, Decimal("100"))   # alice cashes in 100
lend(session, bank, bob, Decimal("300"))        # bank lends bob 300 it lacks

assert total_reserves(bank)   == Decimal("100")  # base money unchanged
assert total_deposits(bank)   == Decimal("400")  # 100 + 300: money grew
assert reserve_ratio(bank)    == Decimal("0.25") # fractional reserve
```

## The bank run

A fractional bank owes more deposit money than it holds base money — that is
the *intended* state of a bank that lends. Withdrawing past reserves fails with
the engine's `InsufficientFundsError`: the bank cannot honor a deposit it lent
out. `test_withdraw_beyond_reserves_fails` is the bank-run test. The book is
left intact on a failed withdrawal (the debit happens only after the `transfer`
succeeds).

## Interest accrual (`ctx.tick`)

`bank.lua` accrues simple interest each tick from `ctx.tick` (Step 5a):
`interest_due += principal * rate * elapsed`, where `elapsed = ctx.tick -
last_accrued_tick`. Because it reads the **wall tick**, a compute-budget skip
does not lose interest — the next run catches up in one go
(`test_interest_is_skip_safe`). Repayment reads `interest_due` (current as of
the last tick the script ran); interest accrues at tick boundaries, the way a
daily-accrual loan settles intraday at the last midnight.

## The liquidity rule (`reserve_floor.lua`)

Bind this as a VALIDATOR on the **bank entity**. It fires only for the bank's
*own outbound transfers* (a `transfer`'s `op.entity_id` is the sender's; an
entity-bound validator keeps to its entity). It enforces a reserve floor: a
withdrawal that would push reserves below the floor is vetoed.

The floor is **two-layered** (the decision-rule/data-effect split):
- a `DEFAULT_FLOOR` in source — changing it is a constitutional act (re-enact
  via `set_validator` at supermajority);
- a governed override read live each op from the `bank:reserve_floor`
  WorldSetting (`{"floor": "<money>"}`) via `ctx.query.world_setting` (Step 5c).

### Why a floor on withdrawals, not a cap on lending

The bank creates money by **lending** — but lending is a *book entry* in script
state, not an engine operation. There is **no `lend` intent** for a VALIDATOR to
gate. The engine cannot constitutionally constrain a book entry, because a book
entry is not an engine op. A **reserve requirement** (cap lending at a ratio of
deposits) must live in the bank's *own* script or a platform audit, not in an
engine VALIDATOR. What *is* an engine op is a `transfer` of base money out of
reserves — and that is what this floor gates. **That asymmetry is itself the
lesson: credit money lives outside the engine's enforcement surface by
construction.** (A world that wants a hard cap on how much a bank may lend puts
it in `lend()` itself, as a self-check against `total_deposits`.)

## Interbank settlement

`interbank_pay()` pays a depositor who banks **elsewhere**: the intra-bank book
is not enough, so the two banks settle on the reserve layer — base money
(`transfer`) moves from the payer's bank to the payee's bank, and each adjusts
its own deposit book. `test_interbank_payment_settles_in_base_money` is the
interbank test. If the payer's bank lent out its reserves, the settlement fails
— an interbank liquidity squeeze.

## Limitations & extensions

- **Demand deposits only.** No term deposits / time locks (a maturity would use
  `ctx.tick`, as the bond does).
- **Simple interest, bullet repayment.** Interest accrues on original principal
  to maturity; partial repayment does not reduce future interest. An amortising
  loan would reduce `principal` on each repayment.
- **One reserve account.** The bank keeps one reserve account in one currency.
- **No deposit insurance / lender of last resort.** A bank run is a hard fail
  (`InsufficientFundsError`); a central-bank backstop would be a POLICY script
  that `issue_money`s into a failing bank's reserves (gated by
  `monetization_cap`).

## When this stops being enough (Fork B)

Fork A covers a bank whose deposits are an undifferentiated claim (every
deposit is fungible with every other). It stops being enough the day a bank
issues **non-fungible, traded** claims — a securitised loan with *its*
collateral, a structured product with *its* waterfall. That is the trigger to
re-open Step 5b as Fork B (a first-class `Position`/`Instrument` model). Until
then: a deposit is a number in a book, and that is enough.
