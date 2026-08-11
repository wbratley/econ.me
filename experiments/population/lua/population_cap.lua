-- population_cap.lua  (VALIDATOR, global)
--
-- The world's VOTABLE population ceiling (tier C) -- a cap the world sets
-- on itself, tighter than the operator's non-votable server cap (tier B,
-- which lives in the engine and cannot be voted out). Reads
-- ctx.query.population() -- the 6c active-count query -- BEFORE the child
-- exists, and vetoes once the living count reaches the cap.
--
-- Created AFTER birth_law, so for an eligible couple this fires second:
-- the birth-law says "yes, these are valid parents", then this cap says
-- "...but the world is full". The two rules are independent validators,
-- each returnable as a distinct veto reason.
--
-- This is nothing but a read of ctx.query.population() and a veto -- the
-- same shape as age_gate.lua in the lifecycle demo reading age().

if ctx.op.type ~= "spawn_entity" then return true end

local cap = tonumber(ctx.state.population_cap)
if ctx.query.population() >= cap then
  return { allow = false,
           reason = "population cap reached (" .. cap .. ")" }
end

return true
