-- birth_law.lua  (VALIDATOR, global)
--
-- The world's birth rule -- nothing but reads of ctx.query, composed. The
-- engine ships NONE of this semantics; a world writes it. Created BEFORE
-- population_cap so it fires first: an ineligible birth is refused on its
-- own demerits before capacity is even considered.
--
-- On a spawn_entity op, checks (in order, first failure wins):
--   1. exactly two parents              (the parents list length)
--   2. distinct                         (no self-fathering)
--   3. one MALE + one FEMALE holding    (sex is DATA -- a holding, not a
--                                        field; the robot counterexample is
--                                        why the engine has no sex column)
--   4. both of age (MIN_PARENT_AGE)     (age() -- the 6a keystone, reused)
--   5. married to each other            (a WorldSetting registry -- the
--                                        marriage DATUM, readable here
--                                        because validators cannot read
--                                        another script's state)
--
-- Passes anything that isn't a spawn. Fail-closed throughout: a missing
-- datum (nil age, no marriage record) DENIES rather than silently admits.
-- That is the safe default for any eligibility rule.

if ctx.op.type ~= "spawn_entity" then return true end

local p = ctx.op.parents
if p == nil or #p ~= 2 then
  return { allow = false, reason = "birth requires exactly two parents" }
end

local p1, p2 = p[1], p[2]
if p1 == p2 then
  return { allow = false, reason = "parents must be distinct" }
end

-- Sex: exactly one male-holder paired with one female-holder (either way).
local m1 = tonumber(ctx.query.holding(p1, "MALE")) > 0
local m2 = tonumber(ctx.query.holding(p2, "MALE")) > 0
local f1 = tonumber(ctx.query.holding(p1, "FEMALE")) > 0
local f2 = tonumber(ctx.query.holding(p2, "FEMALE")) > 0
if not ((m1 and f2) or (m2 and f1)) then
  return { allow = false, reason = "birth requires one male and one female" }
end

-- Age: both parents of age. age() is nil for entities that predate tracking
-- -- fail-closed (cannot certify an unknown-age parent eligible).
local min_age = tonumber(ctx.state.min_parent_age)
local a1 = ctx.query.age(p1)
local a2 = ctx.query.age(p2)
if a1 == nil or a2 == nil or a1 < min_age or a2 < min_age then
  return { allow = false, reason = "both parents must be of age" }
end

-- Marriage: a mutual WorldSetting registry. married:P1 = {spouse=P2} AND
-- married:P2 = {spouse=P1}. Asymmetric or absent records deny.
local mar1 = ctx.query.world_setting("married:" .. p1)
local mar2 = ctx.query.world_setting("married:" .. p2)
if mar1 == nil or mar2 == nil
   or mar1.spouse ~= p2 or mar2.spouse ~= p1 then
  return { allow = false, reason = "parents must be married to each other" }
end

return true
