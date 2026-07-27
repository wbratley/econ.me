"""Pure-Python metrics for the inequality experiment -- no numpy/pandas
anywhere in this repo's .venv, so Gini/percentile/share math is hand-rolled.
Queries the session directly (this harness has no HTTP layer)."""

from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine.markets import get_market
from econengine.models import (
    Entity, EntityStatus, Facility, Holding, Need, NeedState, Parcel, Tick,
)

from .scenario import Scenario

_PRICED_GOODS = ("FOOD", "CLOTHES", "TOOLS", "LABOR", "LABOR-FARM")


def gini(values: list[float]) -> float:
    """Standard Gini coefficient. 0 = perfect equality, ~1 = maximal
    inequality. Returns 0 for an empty or all-zero population."""
    xs = sorted(v for v in values if v >= 0)
    n = len(xs)
    total = sum(xs)
    if n == 0 or total == 0:
        return 0.0
    cumulative = sum((i + 1) * x for i, x in enumerate(xs))
    return (2 * cumulative) / (n * total) - (n + 1) / n


def wealth_share(values: list[float], top_fraction: float) -> float:
    """Share of total wealth held by the richest `top_fraction` of the
    population (e.g. 0.10 for the top 10%)."""
    xs = sorted(values)
    total = sum(xs)
    if not xs or total == 0:
        return 0.0
    n = len(xs)
    k = max(1, round(n * top_fraction))
    return sum(xs[n - k:]) / total


def bottom_share(values: list[float], bottom_fraction: float) -> float:
    xs = sorted(values)
    total = sum(xs)
    if not xs or total == 0:
        return 0.0
    n = len(xs)
    k = max(1, round(n * bottom_fraction))
    return sum(xs[:k]) / total


def _last_prices(session: Session) -> dict[str, float | None]:
    prices = {}
    for symbol in _PRICED_GOODS:
        market = get_market(session, symbol)
        prices[symbol] = float(market.last_price) if market and market.last_price else None
    return prices


def _holdings_value(session: Session, entity_id: str, prices: dict[str, float | None]) -> float:
    rows = session.execute(
        select(Holding).where(Holding.entity_id == entity_id)
    ).scalars().all()
    total = 0.0
    for h in rows:
        price = prices.get(h.symbol)
        if price:
            total += float(h.quantity) * price
    return total


def _market_activity(session: Session, tick_number: int) -> dict[str, dict[str, float]]:
    """Per-symbol ordered-vs-filled quantity for one tick, read back off the
    tick's own event log. This is the diagnostic that distinguishes a price
    that is genuinely clearing from one that is merely moving: a market where
    quoted supply persistently exceeds what fills is rationed by quantity, not
    priced by it -- exactly the fixed-employer-count labor glut that a price
    series alone hid completely."""
    tick = session.execute(
        select(Tick).where(Tick.number == tick_number)
    ).scalar_one_or_none()
    if tick is None:
        return {}

    activity: dict[str, dict[str, float]] = {}

    def bucket(symbol: str) -> dict[str, float]:
        return activity.setdefault(
            symbol,
            {"buy_ordered": 0.0, "sell_ordered": 0.0, "buy_filled": 0.0, "sell_filled": 0.0},
        )

    for event in tick.events or []:
        if event.get("type") == "place_order" and event.get("status") == "applied":
            params = event.get("params") or {}
            side = params.get("side")
            if side in ("buy", "sell"):
                bucket(params.get("symbol", ""))[f"{side}_ordered"] += float(params.get("quantity", 0))
        elif event.get("type") == "trade":
            side = event.get("side")
            if side in ("buy", "sell"):
                bucket(event.get("market", ""))[f"{side}_filled"] += float(event.get("quantity", 0))

    for stats in activity.values():
        for side in ("buy", "sell"):
            ordered = stats[f"{side}_ordered"]
            stats[f"{side}_fill_ratio"] = (stats[f"{side}_filled"] / ordered) if ordered else None
    return activity


def _production(session: Session, tick_number: int) -> dict[str, float]:
    """What the economy actually grew and made this tick, from the tick's own
    `process_completed` events. Output is the quantity that matters when the
    question is whether productive capacity is being lost -- a price series
    cannot tell a shortage from a change in willingness to pay."""
    tick = session.execute(
        select(Tick).where(Tick.number == tick_number)
    ).scalar_one_or_none()
    if tick is None:
        return {}
    produced: dict[str, float] = {}
    for event in tick.events or []:
        if event.get("type") != "process_completed":
            continue
        for symbol, quantity in (event.get("outputs") or {}).items():
            produced[symbol] = produced.get(symbol, 0.0) + float(quantity)
    return produced


def _farmland(session: Session, scenario: Scenario) -> dict[str, int]:
    """Working vs idle farmland.

    The estate rule moves a dead entity's parcels to its recipient, but the
    Treasury has no production script -- it redistributes cash and nothing
    else. So every field the Treasury inherits stops growing food entirely,
    permanently, and the economy's productive capacity ratchets down with
    each death. Fields held by incapacitated entities are dead the same way.
    Counting them is the difference between "people are poor" and "there is
    less food in the world than there was"."""
    working = idle = treasury_held = 0
    parcels = session.execute(
        select(Parcel).join(Facility, Facility.parcel_id == Parcel.id)
        .where(Facility.facility_type == "FARM")
    ).scalars().unique().all()
    for parcel in parcels:
        owner = session.get(Entity, parcel.owner_id) if parcel.owner_id else None
        if owner is None:
            idle += 1
        elif owner.id == scenario.treasury_id:
            treasury_held += 1
            idle += 1
        elif owner.status != EntityStatus.ACTIVE:
            idle += 1
        else:
            working += 1
    return {"working": working, "idle": idle, "treasury_held": treasury_held}


def _holding_qty(session: Session, entity_id: str, symbol: str) -> float:
    holding = session.execute(
        select(Holding).where(Holding.entity_id == entity_id, Holding.symbol == symbol)
    ).scalar_one_or_none()
    return float(holding.quantity) if holding else 0.0


def _satisfaction(session: Session, entity_id: str, need_code: str) -> float:
    need = session.execute(select(Need).where(Need.code == need_code)).scalar_one_or_none()
    if need is None:
        return 0.0
    state = session.execute(
        select(NeedState).where(NeedState.entity_id == entity_id, NeedState.need_id == need.id)
    ).scalar_one_or_none()
    return float(state.satisfaction) if state else 0.0


def snapshot(session: Session, scenario: Scenario, tick_number: int) -> dict:
    """Per-tick aggregate + per-individual metrics."""
    prices = _last_prices(session)

    entities: list[dict] = []
    for entity_id in scenario.individual_ids:
        entity = session.get(Entity, entity_id)
        account = entity.accounts[0] if entity.accounts else None
        cash = float(account.balance) if account else 0.0
        net_worth = cash + _holdings_value(session, entity_id, prices)
        entities.append({
            "entity_id": entity_id,
            "status": entity.status.value,
            "cash": cash,
            "net_worth": net_worth,
            "hunger_satisfaction": _satisfaction(session, entity_id, "HUNGER"),
            "comfort_satisfaction": _satisfaction(session, entity_id, "COMFORT"),
            # COND-WEAK has no decay_per_tick, so it never recovers: it is a
            # running total of every tick this person went hungry, and any
            # positive amount already costs them 30% of their labor. Tracking
            # the stock (not just who has crossed the threshold) is what makes
            # a slow slide toward incapacity visible before it happens.
            "cond_weak": _holding_qty(session, entity_id, "COND-WEAK"),
            "incapacitated_tick": entity.incapacitated_tick,
            "landed": scenario.landed.get(entity_id, False),
            "starting_balance": float(scenario.starting_balance.get(entity_id, 0)),
            "starting_skill": float(scenario.starting_skill.get(entity_id, 0)),
        })

    net_worths = [e["net_worth"] for e in entities]
    incapacitated = sum(1 for e in entities if e["status"] == EntityStatus.INCAPACITATED.value)
    treasury = session.get(Entity, scenario.treasury_id)

    return {
        "tick": tick_number,
        "prices": prices,
        "markets": _market_activity(session, tick_number),
        "gini": gini(net_worths),
        "mean_net_worth": (sum(net_worths) / len(net_worths)) if net_worths else 0.0,
        "median_net_worth": median(net_worths) if net_worths else 0.0,
        "top10_share": wealth_share(net_worths, 0.10),
        "bottom50_share": bottom_share(net_worths, 0.50),
        "incapacitated_count": incapacitated,
        "mean_hunger_satisfaction": (
            sum(e["hunger_satisfaction"] for e in entities) / len(entities) if entities else 0.0
        ),
        "cond_weak_total": sum(e["cond_weak"] for e in entities),
        "cond_weak_carriers": sum(1 for e in entities if e["cond_weak"] > 0),
        "produced": _production(session, tick_number),
        "farmland": _farmland(session, scenario),
        "treasury_balance": (
            float(treasury.accounts[0].balance) if treasury and treasury.accounts else 0.0
        ),
        "entities": entities,
    }


def mobility_correlation(first_snapshot: dict, last_snapshot: dict) -> float:
    """Pearson correlation between starting net worth (first tick) and
    ending net worth (last tick) across individuals -- the actual "does your
    starting position determine your ending position" answer. 1 = rigid
    hierarchy, 0 = no relationship, negative = reversal."""
    first_by_id = {e["entity_id"]: e["net_worth"] for e in first_snapshot["entities"]}
    xs, ys = [], []
    for e in last_snapshot["entities"]:
        if e["entity_id"] in first_by_id:
            xs.append(first_by_id[e["entity_id"]])
            ys.append(e["net_worth"])
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    return cov / (var_x ** 0.5 * var_y ** 0.5)
