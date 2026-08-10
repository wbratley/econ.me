-- age_gate.lua  (VALIDATOR, global)
--
-- The labor law, enforced by the engine's validator layer: only citizens of
-- working age (MIN_WORK_AGE <= age < RETIRE_AGE) are taxed as workers.
-- Minors and retirees are exempt -- they have no wage to tax. This is the
-- AGE-GATE instrument from Step 6b: a piece of world policy that is nothing
-- but a read of ctx.query.age() and a veto.
--
-- What it reads: the SENDER's age. For a poll-tax transfer the sender is the
-- citizen (ctx.op.entity_id), so age() resolves directly -- no oracle, no
-- account-to-entity lookup. Pension and grant transfers carry different
-- references ("pension", "grant") and pass through untouched (the gate only
-- touches "poll-tax").
--
-- Fail-closed: an entity with no birth_tick (nil age -- predates tracking, or
-- untracked) cannot be certified eligible, so the tender is denied rather
-- than silently admitted. That is the safe default for any age-gating rule.

local n = ctx.op
if n.type ~= "transfer" then return true end
if n.reference ~= "poll-tax" then return true end

local age = ctx.query.age(n.entity_id)
if age == nil then
  return {allow = false,
          reason = "age unknown (entity predates age-tracking)"}
end

local min_work = tonumber(ctx.state.min_work_age)
local retire   = tonumber(ctx.state.retire_age)

if age < min_work then
  return {allow = false,
          reason = "minor: age " .. age .. " < " .. min_work}
end
if age >= retire then
  return {allow = false,
          reason = "retiree: age " .. age .. " >= " .. retire}
end

return true
