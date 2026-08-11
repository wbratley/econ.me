-- citizen.lua  (BEHAVIOUR, bound to each individual)
--
-- The citizen's sole action each tick: tender the poll tax to the treasury.
-- Whether it actually PAYS is decided by the age_gate validator (working age
-- only); a vetoed tender surfaces as a clean "rejected" event, NOT a script
-- error, because ctx.action.* only QUEUES an intent -- it does not execute
-- inline. Intents resolve after the script finishes, each in its own
-- savepoint. So this script runs cleanly every tick regardless of age, and
-- the validator's verdict is the observable: did the citizen pay (working
-- age) or was the tender rejected (minor / retiree)?
--
-- This is the whole point of 6b: a script whose economic meaning depends
-- entirely on ctx.query.age() -- which it never calls directly. The gate
-- reads age; the citizen just lives its life.

ctx.action.transfer(ctx.accounts[1].id, ctx.state.treasury_account_id,
                    ctx.state.poll_tax, "poll-tax")
