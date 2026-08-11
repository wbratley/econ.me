-- endowment.lua  (HOOK, bound to the Government)
--
-- Proves the 6c design point "endowment is a transfer, NOT a mechanism":
-- spawn_entity always opens the newborn's account at ZERO -- it never
-- endows. Starting wealth is a transfer a HOOK (or the spawning script)
-- makes AFTER a successful birth. This hook moves ENDOWMENT from the
-- treasury to the child.
--
-- A HOOK only runs after a SUCCESSFUL operation, so vetoed births (cap,
-- wrong parents) never reach it -- only children who were actually born
-- get endowed. The queued transfer resolves INLINE (same tick), suppressed
-- so no validator/hook re-fires on it. The child's balance goes 0 -> 100
-- the very tick it is born.
--
-- ctx.op carries the spawn summary: child_id and account_id (the newborn's
-- empty account). ctx.entity is the Government (the caller), so ctx holds
-- its accounts -- the treasury it transfers FROM. Ownership invariant holds.

ctx.action.transfer(ctx.state.treasury_account_id,
                    ctx.op.account_id,
                    ctx.state.endowment,
                    "endowment")
