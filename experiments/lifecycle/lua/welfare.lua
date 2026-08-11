-- welfare.lua  (POLICY, bound to the Government)
--
-- The welfare state, driven entirely by ctx.query.age(). Two instruments:
--
--   PENSION          every citizen at/over RETIRE_AGE collects PENSION each
--                    tick, for as long as they live.
--   COMING-OF-AGE    the FIRST tick a citizen reaches MIN_WORK_AGE, a
--                    one-time GRANT. Recorded in came_of_age so it never
--                    repeats -- the nested write
--                    (ctx.state.came_of_age[id] = true) is captured on the
--                    script's state read-back, so it persists across ticks.
--
-- Both transfer OUT of the treasury -- the government's own account -- so
-- the ownership invariant holds: a script only ever moves money from
-- accounts its entity owns. (The poll-tax flows the other way, citizen ->
-- treasury, but that is the CITIZEN's behaviour tendering it, not this
-- policy reaching into anyone's account.)
--
-- Pension is unconditional on the grant or the tax: a retiree who never
-- came of age in this world (joined late, already old) still collects, and a
-- senior pays no poll-tax (the age_gate sees to that). age() is the single
-- input; everything else is bookkeeping.

local treasury    = ctx.state.treasury_account_id
local pension     = ctx.state.pension
local grant       = ctx.state.grant
local retire_age  = tonumber(ctx.state.retire_age)
local min_work_age = tonumber(ctx.state.min_work_age)

for _, c in ipairs(ctx.state.citizens) do
  local age = ctx.query.age(c.entity)
  if age ~= nil then
    if age >= retire_age then
      ctx.action.transfer(treasury, c.account, pension, "pension")
    end
    if age >= min_work_age and ctx.state.came_of_age[c.entity] == nil then
      ctx.state.came_of_age[c.entity] = true
      ctx.action.transfer(treasury, c.account, grant, "grant")
    end
  end
end
