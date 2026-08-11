-- midwife.lua  (POLICY, bound to the Government)
--
-- The state's spawn driver. Each tick it attempts TWO births, exercising
-- both tier-C rules (the world's votable policy over spawn_entity):
--
--   VALID     Adam x Eve     -- a married couple, opposite sex, of age.
--                            Admitted until the population cap vetoes it.
--   ILLICIT   Adam x Lilith  -- Adam is married to Eve, NOT to Lilith.
--                            Always vetoed by the birth_law ("not married").
--
-- ctx.action.spawn_entity only QUEUES an intent (it resolves AFTER this
-- script finishes, each in its own savepoint), so a vetoed birth is a clean
-- "rejected" event -- never a script error. This POLICY therefore runs
-- cleanly every tick regardless of outcomes, exactly like the lifecycle
-- demo's citizen tendering a poll-tax that may be vetoed.
--
-- The caller is the Government (it holds SPAWN); the PARENTS are explicit.
-- That separation is the whole point of 6c: capability gates the CALLER,
-- validators gate the PARENTS. The state is the midwife; Adam and Eve are
-- the parents.

local adam   = ctx.state.adam_id
local eve    = ctx.state.eve_id
local lilith = ctx.state.lilith_id

ctx.action.spawn_entity({ adam, eve },
                        { name = "Child-" .. ctx.tick, currency = "USD" })
ctx.action.spawn_entity({ adam, lilith },
                        { name = "Reject-" .. ctx.tick, currency = "USD" })
