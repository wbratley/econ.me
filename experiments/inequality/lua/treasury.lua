-- Treasury: redistributes its own collected balance (voluntary tax
-- remittances -- see individual.lua) equally across the population every
-- ctx.state.redistribution_period ticks. It can only ever move money OUT OF
-- ITS OWN account -- the ownership invariant means it could never reach into
-- an Individual's account to collect, even if it wanted to.

local account = ctx.accounts[1]
local balance = tonumber(account.balance)

local period = tonumber(ctx.state.redistribution_period) or 5
local counter = (ctx.state.counter or 0) + 1
ctx.state.counter = counter

if counter >= period then
  ctx.state.counter = 0
  local recipients = ctx.state.recipients or {}
  local n = #recipients
  if n > 0 and balance > 0 then
    local share = balance / n
    if share > 0.01 then
      for _, recipient_account_id in ipairs(recipients) do
        ctx.action.transfer(account.id, recipient_account_id,
                             string.format("%.4f", share), "ubi", 5)
      end
    end
  end
end
