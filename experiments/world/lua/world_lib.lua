-- World lib (docs/scripting.md section 2): engine idioms shared by every
-- script in THIS world, injected as the `world` namespace. Stored as the
-- `scripting.world_lib` WorldSetting (set at bootstrap by
-- scenario.create_content; read per tick by the engine). No play opinions
-- here -- strategies live in the content pack (pack.lua).
--
-- This chunk RETURNS its namespace table.

local world = {}

-- Order feedback: cancel last tick's own orders, report fill ratios. -----
-- Orders are good-till-cancelled, so each script cancels the generation it
-- placed last tick and reads how it did. Returns fills["SYMBOL|side"] with
-- {ordered, filled, ratio}. Sell-side adaptation (in the content pack's
-- `concede`) consumes the ratio.
function world.settle_last_orders()
  local by_order, fills = {}, {}

  for _, e in ipairs(ctx.events) do
    if e.type == "place_order" and e.status == "applied" and e.order_id then
      ctx.action.cancel_order(e.order_id, 1)
      by_order[e.order_id] = {
        key = e.params.symbol .. "|" .. e.params.side,
        ordered = tonumber(e.params.quantity) or 0,
        filled = 0,
      }
    end
  end
  for _, e in ipairs(ctx.events) do
    if e.order_id and by_order[e.order_id] and e.type == "trade" then
      by_order[e.order_id].filled = by_order[e.order_id].filled
        + (tonumber(e.quantity) or 0)
    end
  end
  for _, o in pairs(by_order) do
    local f = fills[o.key] or { ordered = 0, filled = 0 }
    f.ordered = f.ordered + o.ordered
    f.filled = f.filled + o.filled
    fills[o.key] = f
  end
  for key, f in pairs(fills) do
    if f.ordered > 0 then f.ratio = f.filled / f.ordered
    else fills[key] = nil end
  end
  return fills
end

return world
