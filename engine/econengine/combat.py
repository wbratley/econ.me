"""Combat — entity versus entity, stats into outcomes.

Creatures are entities. What a creature IS lives in entity_stats rows
(ATTACK, DEFENSE); what it CARRIES lives in holdings — a spear is not a
stat, it is a held good the rules price. The rules themselves are the
pack's COMBAT_RULES world setting:

    {"night_only": true,            # refused by daylight, a clear error
     "deterrence": {"WARMTH": 1},   # holdings that turn attackers away
     "weapons": {"SPEAR": 3},       # attack bonus per unit held
     "armor": {"CLOTHES": 1},       # defense bonus per unit held
     "loot": {"*": 1, "MEAT": 3},  # "*": the estate, to CARRYing victors
     "carry_stat": "CARRY",        # victors with this stat seize "*"
     "bite_loot": {"MEAT": 1},      # a landed bite feeds the attacker
     "base_hit": 50, "per_point": 5}

Resolution: hit% = clamp(base_hit + per_point x (ATK - DEF), 5, 95),
damage = max(1, ATK - DEF) (+1 on a roll >= 90), rolled on the
commit-reveal RNG (sha over the previous tick's events hash). Health is
two-layered: innate HITS is a STAT row -- world-assigned at spawn,
immutable, and what marks a creature as fightable at all (an entity
cannot opt out of combat by shedding a holding); current health is
the HITS holding, drained by damage, never regrown. A defender at
zero crosses into the existing incapacity/estate machinery -- dying
to a wolf is dying, one rule.

A kill is a carcass. The declared per-symbol loot (MEAT) is torn
from it by ANY victor -- a wolf eats what it killed. The "*" entry
is the estate: everything the dead carried (holdings and purse)
moves to the victor, but only if the victor can CARRY (the
carry_stat) -- there is no world-location layer yet, and a wolf
toting the trader's shelf is wrong physics; what a beast kills
rots where it fell, what a person kills is inheritance.

Deterrence is a MISS, not a refusal: the attack happens, the world
hears it, nobody bleeds — firelight turns the pack at the door. That is
the whole defense ladder of the stone world: fire and shelter keep
wolves at bay; otherwise it is weapons.

Fighting is up close on a mapped world (docs/spatial.md S4): when both
fighters are placed, they must share a spot — and a traveller mid-hop
stands at the hop's origin until arrival, so roads are fought over
place by place. Unplaced fighters (and mapless worlds, where everyone
is unplaced) keep the global night of the pre-spatial engine.

Combat events are loud facts (witness.py): every rival hears who
hunted whom. An attack with no target (attack(nil) — a desperate
prowl) picks the noisiest speaker of the night so far, else a random
active individual: speech is free by day, but at night it has a price.
"""

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import clock, conditions, rng
from .models import (
    Entity, EntityStatus, EntityType, EntityStat, Holding, Tick, WorldSetting,
)

_QUANTUM = Decimal("0.0001")

COMBAT_RULES_KEY = "combat.rules"

_DEFAULT_RULES: dict = {}


def set_rules(session: Session, rules: dict) -> None:
    row = session.get(WorldSetting, COMBAT_RULES_KEY)
    if row is None:
        session.add(WorldSetting(key=COMBAT_RULES_KEY, value=rules))
    else:
        row.value = rules


def get_rules(session: Session) -> dict:
    row = session.get(WorldSetting, COMBAT_RULES_KEY)
    return dict(row.value) if row is not None else dict(_DEFAULT_RULES)


# ---------------------------------------------------------------------------
# Stats

def create_stat(session: Session, entity_id: str, stat: str,
                value: Decimal) -> EntityStat:
    row = EntityStat(entity_id=entity_id, stat=str(stat).upper(),
                     value=Decimal(value).quantize(_QUANTUM))
    session.add(row)
    session.flush()
    return row


def get_stats(session: Session, entity_id: str) -> dict[str, Decimal]:
    rows = session.execute(
        select(EntityStat).where(EntityStat.entity_id == entity_id)
        .order_by(EntityStat.stat)
    ).scalars().all()
    return {r.stat: r.value for r in rows}


def _holding_qty(session: Session, entity_id: str, symbol: str) -> Decimal:
    h = session.execute(
        select(Holding).where(Holding.entity_id == entity_id,
                              Holding.symbol == symbol)
    ).scalar_one_or_none()
    return h.quantity if h is not None else Decimal("0")


def effective_attack(session: Session, entity_id: str) -> Decimal:
    rules = get_rules(session)
    atk = get_stats(session, entity_id).get("ATTACK", Decimal("0"))
    for symbol, bonus in (rules.get("weapons") or {}).items():
        atk += Decimal(bonus) * _holding_qty(session, entity_id, symbol)
    return atk


def effective_defense(session: Session, entity_id: str) -> Decimal:
    rules = get_rules(session)
    dfn = get_stats(session, entity_id).get("DEFENSE", Decimal("0"))
    for symbol, bonus in (rules.get("armor") or {}).items():
        dfn += Decimal(bonus) * _holding_qty(session, entity_id, symbol)
    return dfn


# ---------------------------------------------------------------------------
# Resolution

def _deterred(session: Session, rules: dict, entity_id: str) -> bool:
    for symbol, floor in (rules.get("deterrence") or {}).items():
        if _holding_qty(session, entity_id, symbol) >= Decimal(floor):
            return True
    return False


def pick_prey(session: Session, tick_number: int,
              exclude_id: str | None = None) -> str | None:
    """A desperate prowl's target: the noisiest speaker of the night so
    far, else a random active individual. Noise is how predators find
    you; silence makes you hard to find, never safe.

    A PLACED predator prowls its own ground only (docs/spatial.md S4):
    the loudest speaker it can actually reach is the loudest speaker
    standing where it stands. An unplaced predator hears the whole
    world -- the legacy prowl."""
    scope_id = None
    if exclude_id is not None:
        hunter = session.get(Entity, exclude_id)
        if hunter is not None:
            scope_id = hunter.location_place_id
    candidates = session.execute(
        select(Entity.id).where(
            Entity.status == EntityStatus.ACTIVE,
            Entity.entity_type == EntityType.INDIVIDUAL,
            Entity.id != exclude_id,
            *([] if scope_id is None else
              [Entity.location_place_id == scope_id]),
        ).order_by(Entity.id)
    ).scalars().all()
    rows = session.execute(
        select(Tick).where(Tick.number > max(0, tick_number - 12),
                           Tick.number < tick_number)
        .order_by(Tick.number)
    ).scalars().all()
    says: dict[str, int] = {}
    for row in rows:
        for event in (row.events or []):
            if (event.get("type") == "say"
                    and event.get("status") != "rejected"
                    and event.get("entity_id")
                    and event["entity_id"] != exclude_id
                    and event["entity_id"] in candidates):
                says[event["entity_id"]] = says.get(event["entity_id"], 0) + 1
    if says:
        return max(sorted(says), key=lambda k: says[k])
    if not candidates:
        return None
    roll = int(rng.outcome_roll(
        _prev_hash(session, tick_number), f"prowl:{tick_number}"), 16)
    return candidates[roll % len(candidates)]


def _seize_estate(session: Session, defender: Entity,
                  attacker: Entity) -> dict[str, str]:
    """Everything the dead carried moves to the victor: property
    (non-condition holdings) and purse. Conditions were the body, not
    the estate — they burn with it. Must run BEFORE _incapacitate,
    whose estate pass disposes of whatever remains."""
    from . import markets
    from .models import Account, Good

    condition_symbols = {
        symbol for (symbol,) in session.execute(
            select(Good.symbol).where(
                (Good.modifies_pattern.is_not(None))
                | (Good.incapacitates_at.is_not(None))
            )
        ).all()
    }
    taken: dict[str, str] = {}
    for holding in session.execute(
        select(Holding).where(Holding.entity_id == defender.id,
                              Holding.quantity > 0)
        .order_by(Holding.symbol)
    ).scalars():
        if holding.symbol not in condition_symbols:
            markets.adjust_holding(session, attacker, holding.symbol,
                                   holding.quantity)
            taken[holding.symbol] = str(holding.quantity.quantize(_QUANTUM))
        holding.quantity = Decimal("0")
    for account in session.execute(
        select(Account).where(Account.entity_id == defender.id,
                              Account.balance != 0)
        .order_by(Account.currency, Account.id)
    ).scalars():
        conditions._credit_account(session, attacker, account.currency,
                                   account.balance)
        taken[account.currency] = str(account.balance.quantize(_QUANTUM))
        account.balance = Decimal("0")
    return taken


def _prev_hash(session: Session, tick_number: int) -> str:
    row = session.execute(
        select(Tick).where(Tick.number == tick_number - 1)
    ).scalar_one_or_none()
    return rng.hash_events(row.events or []) if row is not None else rng.GENESIS_HASH


def is_creature(session: Session, entity_id: str) -> bool:
    """Creature-ness is a STAT, not a holding: the world assigns HITS
    at spawn and no intent can shed it. An entity cannot opt out of
    combat by dumping what it holds — health is what the world says
    it is."""
    return "HITS" in get_stats(session, entity_id)


def resolve_attack(session: Session, attacker_id: str,
                   defender_id: str | None, tick_number: int) -> dict:
    """One creature's attempt on another. Returns the event dict (already
    applied): refused, deterred-miss, or hit with damage; a kill rides
    the incapacity machinery and pays the loot to the victor."""
    from . import markets  # deferred: imports this module

    rules = get_rules(session)
    event: dict = {
        "type": "combat",
        "entity_id": attacker_id,
        "target_id": defender_id,
    }
    attacker = session.get(Entity, attacker_id)
    if attacker is None or attacker.status != EntityStatus.ACTIVE:
        return {**event, "status": "rejected", "reason": "attacker cannot act"}
    if defender_id is None:
        defender_id = pick_prey(session, tick_number, exclude_id=attacker_id)
        if defender_id is None:
            return {**event, "status": "rejected", "reason": "no prey"}
        event["target_id"] = defender_id
    defender = session.get(Entity, defender_id)
    if defender is None or defender.status != EntityStatus.ACTIVE:
        return {**event, "status": "rejected", "reason": "target cannot be fought"}
    if not is_creature(session, defender_id):
        # No innate HITS: infrastructure, scenery, the unspawned. It
        # cannot be fought — creature-ness is declared by the world,
        # never chosen by the entity.
        return {**event, "status": "rejected",
                "reason": "target is not a creature (no HITS stat)"}
    if attacker_id == defender_id:
        return {**event, "status": "rejected", "reason": "cannot attack self"}
    # Co-location (docs/spatial.md S4): hunting is up close. When the
    # world has a map and BOTH fighters stand on it, they must stand at
    # the same spot. The gate fires on declared data only -- an
    # unplaced fighter (or a mapless world, where everyone is unplaced)
    # fights exactly as before: the night stays global for those who
    # are nowhere. A traveller mid-hop stands at the hop's origin
    # until arrival moves them, so the road bites (and is bitten)
    # place by place.
    if (attacker.location_place_id is not None
            and defender.location_place_id is not None
            and attacker.location_place_id != defender.location_place_id):
        from . import places as places_mod

        return {**event, "status": "rejected",
                "reason": (
                    f"{defender.name} is at "
                    f"{places_mod.label(defender.place)} -- you are at "
                    f"{places_mod.label(attacker.place)}; hunting is up close")}
    if rules.get("night_only") and not clock.is_night(tick_number):
        return {**event, "status": "rejected",
                "reason": f"too bright to hunt (hour {clock.hour_of(tick_number)}, "
                          f"daylight is hours 06..19)"}
    event["attack"] = str(effective_attack(session, attacker_id).quantize(
        _QUANTUM, rounding=ROUND_HALF_UP))
    event["defense"] = str(effective_defense(session, defender_id).quantize(
        _QUANTUM, rounding=ROUND_HALF_UP))
    if _deterred(session, rules, defender_id):
        # The hearth turns the pack at the door: a loud miss, not a
        # refusal — the world heard the attempt.
        return {**event, "hit": False, "deterred": True, "damage": "0"}
    atk, dfn = Decimal(event["attack"]), Decimal(event["defense"])
    base = Decimal(str(rules.get("base_hit", 50)))
    per_point = Decimal(str(rules.get("per_point", 5)))
    hit_pct = min(Decimal("95"), max(Decimal("5"), base + per_point * (atk - dfn)))
    roll = int(rng.outcome_roll(
        _prev_hash(session, tick_number),
        f"combat:{attacker_id}:{defender_id}:{tick_number}"), 16) % 10000
    if Decimal(roll) / 100 >= hit_pct:
        return {**event, "hit": False, "damage": "0"}
    damage = max(Decimal("1"), atk - dfn)
    if roll >= 9000:                      # a clean opening: the deep cut
        damage += Decimal("1")
        event["crit"] = True
    hits = _holding_qty(session, defender_id, "HITS")
    dealt = min(damage, hits)
    if dealt > 0:
        markets.adjust_holding(session, defender, "HITS", -dealt)
    # A landed bite feeds the attacker (it tears flesh): the pack that
    # hunts houses eats by the bite, not only by the kill.
    for symbol, qty in sorted((rules.get("bite_loot") or {}).items()):
        markets.adjust_holding(session, attacker, symbol, Decimal(qty))
    event.update(hit=True, damage=str(dealt.quantize(_QUANTUM)),
                 target_hits=str(max(Decimal("0"), hits - dealt)
                                 .quantize(_QUANTUM)))
    if hits - dealt <= 0:
        event["killed"] = True
        # A kill is a carcass. The estate ("*") moves only to a
        # victor that can CARRY — seized BEFORE the incapacity pass
        # burns what remains. What a beast kills rots where it fell.
        loot_rules = rules.get("loot") or {}
        seized: dict[str, str] = {}
        if "*" in loot_rules and \
                rules.get("carry_stat", "CARRY") in get_stats(session, attacker_id):
            seized = _seize_estate(session, defender, attacker)
        death = conditions._incapacitate(
            session, defender, tick_number,
            condition="HITS", quantity=hits - dealt, threshold=Decimal("0"),
        )
        event["condition"] = death.get("condition")
        event["quantity"] = death.get("quantity")
        # Runs 28/29: a wolf-killed house vanished from the event log --
        # hunger deaths emitted entity_incapacitated, HITS deaths did
        # not. resolve_intent returns one event per intent, so the full
        # estate record (the census and witness contract) rides nested
        # here, and tick.py lifts it into the stream.
        event["death"] = death
        for symbol, qty in sorted(loot_rules.items()):
            if symbol == "*":
                continue
            markets.adjust_holding(session, attacker, symbol, Decimal(qty))
            prev = Decimal(seized.get(symbol, "0"))
            seized[symbol] = str((prev + Decimal(qty)).quantize(_QUANTUM))
        if seized:
            event["loot"] = dict(sorted(seized.items()))
    return event
