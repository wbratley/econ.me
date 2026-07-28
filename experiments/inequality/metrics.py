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

from .scenario import _FOOD_PER_FIELD_TICK, Scenario, share_symbol

_PRICED_GOODS = ("FOOD", "CLOTHES", "TOOLS", "LABOR", "LABOR-FARM")

# --- Valuing the two assets that have no market price ----------------------
#
# No script trades parcels or shares, so every SHARE-FIRM-n market sits at
# last_price None and parcels have no market at all. They still have to appear
# in a wealth measure. Leaving them out is not neutral: 4 of 30 people own all
# the productive land, and a net worth blind to that reports `tax_progressive`
# at gini 0.018 -- near-perfect equality -- while it remains true.
#
# LAND is valued as capitalised Ricardian rent: what a field yields per tick,
# less the labour it takes to work it, capitalised at a discount rate.
#
#     rent = expected FOOD yield x P_FOOD  -  labour per field-tick x P_LABOR
#
# That residual is what accrues to the land itself rather than to the worker,
# and it is the same quantity whoever holds the field: a firm collects it as
# its margin (which is exactly `m x yield x P` by construction -- see NOTES
# "Firms with a margin"), a smallholder collects it by not paying themselves a
# wage. Floored at zero, since nobody is forced to work a field that costs
# more in labour than it yields.
#
# SHARES are valued at book -- the firm's net assets (cash + priced goods +
# its own land at the above) over shares outstanding. Deliberately not
# earnings-based: firm earnings here swing from nothing to monopsony rents
# inside one run (see "the circular flow reverses at ~tick 200"), and any
# multiple applied to them would amplify that swing rather than measure it.
_LABOR_PER_FIELD_TICK = 1.0     # FARM_FOOD_HAND burns 1 LABOR-FARM, from 1 LABOR

# A field is worth 100 ticks of its net rent. This is a modelling choice and
# it scales how much land dominates the wealth distribution, so it is exposed
# rather than buried: capitalising a perpetuity at any realistic annual rate
# is meaningless in a world that ends at tick 400, and 100 ticks is a quarter
# of a standard run. Vary it to check a result is not an artifact of the rate.
LAND_DISCOUNT_PER_TICK = 0.01


def field_value(prices: dict[str, float | None],
                discount_per_tick: float = LAND_DISCOUNT_PER_TICK) -> float:
    """Capitalised net rent of one FIELD. See the note above."""
    food = prices.get("FOOD")
    if not food:
        return 0.0
    wage = prices.get("LABOR") or prices.get("LABOR-FARM") or 0.0
    rent = _FOOD_PER_FIELD_TICK * food - _LABOR_PER_FIELD_TICK * wage
    return max(0.0, rent) / discount_per_tick if discount_per_tick > 0 else 0.0


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


def _capital(session: Session, scenario: Scenario, tick_number: int) -> dict[str, float]:
    """The capital-income channel: what firms are worth and what they paid out.

    Firm cash is tracked because this economy's firms decapitalise rather than
    accumulate -- they start with an endowment and bleed it into wages, so the
    sector's cash is a running measure of how much of the economy is still
    being financed by genesis capital rather than by production. Dividends are
    read off the tick's own transfer events, so they measure money that
    actually moved, not money a script intended to move.
    """
    balances = []
    for firm_id in scenario.firm_ids:
        firm = session.get(Entity, firm_id)
        if firm and firm.accounts:
            balances.append(float(firm.accounts[0].balance))

    dividends = 0.0
    tick = session.execute(
        select(Tick).where(Tick.number == tick_number)
    ).scalar_one_or_none()
    for event in (tick.events if tick else None) or []:
        if event.get("type") != "transfer" or event.get("status") != "applied":
            continue
        params = event.get("params") or {}
        if params.get("reference") == "dividend":
            dividends += float(params.get("amount", 0))

    return {
        "firm_cash_total": sum(balances),
        "firm_cash_min": min(balances) if balances else 0.0,
        "firm_cash_max": max(balances) if balances else 0.0,
        "firms_solvent": sum(1 for b in balances if b > 1.0),
        "dividends_paid": dividends,
    }


def _farmland(session: Session, scenario: Scenario) -> tuple[dict[str, int], dict[str, int]]:
    """Working vs idle farmland, plus fields-held per owner.

    The estate rule moves a dead entity's parcels to its recipient, but the
    Treasury has no production script -- it redistributes cash and nothing
    else. So every field the Treasury inherits stops growing food entirely,
    permanently, and the economy's productive capacity ratchets down with
    each death. Fields held by incapacitated entities are dead the same way.
    Counting them is the difference between "people are poor" and "there is
    less food in the world than there was".

    The per-owner breakdown comes out of the same parcel scan rather than a
    second pass: land is one of the three things a person can actually hold
    (with cash and shares), and it was the one the aggregate hid -- "9 fields
    are working" cannot answer whether the same person holds all of them.
    """
    working = idle = treasury_held = 0
    by_owner: dict[str, int] = {}
    parcels = session.execute(
        select(Parcel).join(Facility, Facility.parcel_id == Parcel.id)
        .where(Facility.facility_type == "FARM")
    ).scalars().unique().all()
    for parcel in parcels:
        owner = session.get(Entity, parcel.owner_id) if parcel.owner_id else None
        if owner is not None:
            by_owner[owner.id] = by_owner.get(owner.id, 0) + 1
        if owner is None:
            idle += 1
        elif owner.id == scenario.treasury_id:
            treasury_held += 1
            idle += 1
        elif owner.status != EntityStatus.ACTIVE:
            idle += 1
        else:
            working += 1
    return {"working": working, "idle": idle, "treasury_held": treasury_held}, by_owner


def _share_register(session: Session) -> tuple[dict[str, float], dict[tuple[str, str], float],
                                                dict[str, float]]:
    """The share register in one query: total per entity, per (entity, symbol),
    and shares outstanding per symbol.

    Shares were allocated at genesis and then never measured again, so "who
    owns the firms" was only ever knowable for tick 0 -- which is exactly the
    wrong tick if the question is whether ownership concentrates. Markets exist
    for these symbols, so they can move even though no script currently trades
    them. The per-symbol breakdown is what lets a holding be *valued*: firms
    differ, so one share of a solvent firm is not one share of a broke one.
    """
    rows = session.execute(
        select(Holding).where(Holding.symbol.like("SHARE-FIRM-%"))
    ).scalars().all()
    by_entity: dict[str, float] = {}
    by_entity_symbol: dict[tuple[str, str], float] = {}
    outstanding: dict[str, float] = {}
    for holding in rows:
        quantity = float(holding.quantity)
        by_entity[holding.entity_id] = by_entity.get(holding.entity_id, 0.0) + quantity
        key = (holding.entity_id, holding.symbol)
        by_entity_symbol[key] = by_entity_symbol.get(key, 0.0) + quantity
        outstanding[holding.symbol] = outstanding.get(holding.symbol, 0.0) + quantity
    return by_entity, by_entity_symbol, outstanding


def _share_unit_values(
    session: Session, scenario: Scenario, prices: dict[str, float | None],
    fields_by_owner: dict[str, int], per_field: float, outstanding: dict[str, float],
) -> dict[str, float]:
    """Book value of one share of each SHARE-FIRM-n: the firm's net assets
    (cash + priced goods + its own fields) over shares outstanding.

    Note what this does NOT do: in arms with `share_allocation="none"` no
    shares exist, so firm net assets belong to nobody and appear in no one's
    wealth. That is the model being honest -- those firms genuinely have no
    owners -- but it means total measured household wealth is not comparable
    between share arms and non-share arms. Compare within, not across.
    """
    values: dict[str, float] = {}
    for index, firm_id in enumerate(scenario.firm_ids):
        symbol = share_symbol(index)
        shares = outstanding.get(symbol, 0.0)
        if shares <= 0:
            continue
        firm = session.get(Entity, firm_id)
        if firm is None:
            continue
        cash = float(firm.accounts[0].balance) if firm.accounts else 0.0
        net_assets = (cash + _holdings_value(session, firm_id, prices)
                      + fields_by_owner.get(firm_id, 0) * per_field)
        values[symbol] = max(0.0, net_assets) / shares
    return values


def _land_use(session: Session) -> dict[str, int]:
    """Parcels by what stands on them, plus the ones still bare. Counted per
    PARCEL rather than per facility, since one use per parcel is the rule the
    firm script keeps (see the BUILD_ recipes in scenario.py) and counting
    facilities would quietly hide a breach of it."""
    counts: dict[str, int] = {"BARE": 0}
    for parcel in session.execute(select(Parcel)).scalars().unique().all():
        types = sorted({f.facility_type for f in parcel.facilities})
        key = "+".join(types) if types else "BARE"
        counts[key] = counts.get(key, 0) + 1
    return counts


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
    farmland, fields_by_owner = _farmland(session, scenario)
    shares_by_owner, shares_by_owner_symbol, shares_outstanding = _share_register(session)
    per_field = field_value(prices)
    share_unit_values = _share_unit_values(
        session, scenario, prices, fields_by_owner, per_field, shares_outstanding)
    # Priced once per holder here rather than per entity inside the loop below,
    # which would rescan the whole register for each person -- O(entities x
    # holdings) per snapshot, and population scaling is the axis this harness
    # is expected to grow along (measured linear, 15->120 individuals).
    shares_value_by_owner: dict[str, float] = {}
    for (holder, symbol), quantity in shares_by_owner_symbol.items():
        shares_value_by_owner[holder] = (
            shares_value_by_owner.get(holder, 0.0)
            + quantity * share_unit_values.get(symbol, 0.0)
        )

    entities: list[dict] = []
    for entity_id in scenario.individual_ids:
        entity = session.get(Entity, entity_id)
        account = entity.accounts[0] if entity.accounts else None
        cash = float(account.balance) if account else 0.0
        net_worth = cash + _holdings_value(session, entity_id, prices)
        # The two assets a market price cannot supply. Kept as separate
        # components rather than folded straight into one number, because
        # "who is rich" and "rich in what" turn out to be different questions
        # here -- land and cash diverge hard once the wage market dies.
        land_value = fields_by_owner.get(entity_id, 0) * per_field
        shares_value = shares_value_by_owner.get(entity_id, 0.0)
        entities.append({
            "entity_id": entity_id,
            "status": entity.status.value,
            "cash": cash,
            # `net_worth` stays cash + priced goods, unchanged, because every
            # gini and mobility figure recorded in NOTES.md is measured on it
            # and silently redefining it would invalidate all of them at once.
            # `total_wealth` is the one that sees everything a person owns.
            "net_worth": net_worth,
            "land_value": land_value,
            "shares_value": shares_value,
            "total_wealth": net_worth + land_value + shares_value,
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
            # The three stocks a person can hold, all as of THIS tick rather
            # than genesis: cash (above), land, and capital.
            "fields": fields_by_owner.get(entity_id, 0),
            "shares": shares_by_owner.get(entity_id, 0.0),
            "starting_balance": float(scenario.starting_balance.get(entity_id, 0)),
            "starting_skill": float(scenario.starting_skill.get(entity_id, 0)),
        })

    net_worths = [e["net_worth"] for e in entities]
    total_wealths = [e["total_wealth"] for e in entities]
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
        # The same three on total wealth: cash + goods + land + shares. Land
        # is the one that moves them -- it is held by 4 of 30 people and
        # valued at 100 ticks of net rent, so `gini` and `gini_total` answer
        # noticeably different questions and both are worth having.
        "gini_total": gini(total_wealths),
        "mean_total_wealth": (sum(total_wealths) / len(total_wealths)) if total_wealths else 0.0,
        "median_total_wealth": median(total_wealths) if total_wealths else 0.0,
        "top10_share_total": wealth_share(total_wealths, 0.10),
        "bottom50_share_total": bottom_share(total_wealths, 0.50),
        "wealth_components": {
            "field_value": per_field,
            "cash": sum(e["cash"] for e in entities),
            "goods": sum(e["net_worth"] - e["cash"] for e in entities),
            "land": sum(e["land_value"] for e in entities),
            "shares": sum(e["shares_value"] for e in entities),
        },
        "incapacitated_count": incapacitated,
        "mean_hunger_satisfaction": (
            sum(e["hunger_satisfaction"] for e in entities) / len(entities) if entities else 0.0
        ),
        # Every need, not just hunger. Rent and power are needs the population
        # can fail independently of food, and the whole point of the priority
        # ordering is that they fail FIRST -- an aggregate that only watches
        # hunger would report a comfortable economy while everyone is cold.
        "needs": {
            code: (
                sum(_satisfaction(session, e["entity_id"], code) for e in entities)
                / len(entities) if entities else 0.0
            )
            for code in ("HUNGER", "SHELTER", "POWER", "COMFORT")
        },
        # What the land ended up being used for -- the output of the build
        # mechanic, and the only place the three sectors' competition for a
        # fixed pool is visible as a number.
        "land_use": _land_use(session),
        "cond_weak_total": sum(e["cond_weak"] for e in entities),
        "cond_weak_carriers": sum(1 for e in entities if e["cond_weak"] > 0),
        "produced": _production(session, tick_number),
        "capital": _capital(session, scenario, tick_number),
        "farmland": farmland,
        "treasury_balance": (
            float(treasury.accounts[0].balance) if treasury and treasury.accounts else 0.0
        ),
        "entities": entities,
    }


def mobility_correlation(first_snapshot: dict, last_snapshot: dict,
                          key: str = "net_worth") -> float:
    """Pearson correlation between starting wealth (first tick) and ending
    wealth (last tick) across individuals -- the actual "does your starting
    position determine your ending position" answer. 1 = rigid hierarchy,
    0 = no relationship, negative = reversal.

    `key` selects the wealth measure: "net_worth" (cash + priced goods, the
    default, and what every mobility figure in NOTES.md was measured on) or
    "total_wealth" (also land and shares). They can differ sharply, since
    land is the asset that does not move between people.
    """
    first_by_id = {e["entity_id"]: e.get(key, e["net_worth"])
                   for e in first_snapshot["entities"]}
    xs, ys = [], []
    for e in last_snapshot["entities"]:
        if e["entity_id"] in first_by_id:
            xs.append(first_by_id[e["entity_id"]])
            ys.append(e.get(key, e["net_worth"]))
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
