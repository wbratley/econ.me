"""Combat and spawns — entity vs entity under declared rules (run 20).

Resolution math, deterrence, weapons-as-carried, kill/loot/incapacity,
and the breeding cadence. The pack-level integration (two hunting
packs vs the starter floor) lives in experiments/world/test_stone_age.
"""
import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import combat, goods, markets, spawns
from econengine.models import Base, Entity, EntityStatus, EntityType
from econengine.services import create_entity


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _world(session, **rules_over):
    goods.create_good(session, "HITS")
    goods.create_good(session, "MEAT")
    goods.create_good(session, "SPEAR")
    goods.create_good(session, "WARMTH", decay_per_tick=Decimal("0"))
    goods.create_good(session, "PELT")
    wolf = create_entity(session, "Wolf", EntityType.INDIVIDUAL)
    house = create_entity(session, "House", EntityType.INDIVIDUAL)
    markets.adjust_holding(session, wolf, "HITS", Decimal("12"))
    markets.adjust_holding(session, house, "HITS", Decimal("20"))
    combat.create_stat(session, wolf.id, "ATTACK", Decimal("4"))
    combat.create_stat(session, wolf.id, "DEFENSE", Decimal("1"))
    combat.create_stat(session, house.id, "ATTACK", Decimal("1"))
    combat.create_stat(session, house.id, "DEFENSE", Decimal("1"))
    rules = {"night_only": True, "deterrence": {"WARMTH": 1},
             "weapons": {"SPEAR": 3}, "armor": {"CLOTHES": 1},
             "loot": {"PELT": 1}, "bite_loot": {"MEAT": 1},
             "base_hit": 50, "per_point": 5}
    rules.update(rules_over)
    combat.set_rules(session, rules)
    session.commit()
    return wolf, house


def _hits(session, e):
    return markets.get_holding(session, e.id, "HITS").quantity


def test_stats_are_born_weapons_are_carried(session):
    """The entity-stat split: ATTACK/DEFENSE rows are the creature; the
    spear is a holding the rules price in."""
    wolf, house = _world(session)
    assert combat.effective_attack(session, wolf.id) == Decimal("4")
    assert combat.effective_attack(session, house.id) == Decimal("1")
    markets.adjust_holding(session, house, "SPEAR", Decimal("2"))
    assert combat.effective_attack(session, house.id) == Decimal("7")


def test_daylight_refuses_and_the_hearth_deters(session):
    """No hunting by day (a clear error naming the window), a lit hearth
    is a loud miss, and infrastructure without HITS is not meat: the
    loudest night quoter in the world cannot be fought."""
    wolf, house = _world(session)
    post = create_entity(session, "Post", EntityType.BUSINESS)   # no HITS
    session.commit()
    ev = combat.resolve_attack(session, wolf.id, post.id, 1)
    assert ev["status"] == "rejected" and "not a creature" in ev["reason"]
    ev = combat.resolve_attack(session, wolf.id, house.id, 10)
    assert ev["status"] == "rejected" and "too bright" in ev["reason"]
    markets.adjust_holding(session, house, "WARMTH", Decimal("1"))
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev["deterred"] is True and ev["hit"] is False
    assert _hits(session, house) == Decimal("20")


def test_hits_math_damage_and_the_kill_with_loot(session):
    """hit% = clamp(50 + 5*(ATK-DEF), 5, 95), damage = max(1, ATK-DEF),
    a landed bite feeds the attacker, and zero HITS crosses into the
    ordinary incapacity machinery with the kill loot to the victor."""
    wolf, house = _world(session, night_only=False, base_hit=100)
    for t in range(1, 30):                          # 95% clamp: scan for
        ev = combat.resolve_attack(session, wolf.id, house.id, t)  # a hit
        if ev.get("hit"):
            break
    assert ev["hit"] is True and Decimal(ev["damage"]) == Decimal("3")
    # the bite fed the wolf
    assert markets.get_holding(session, wolf.id, "MEAT").quantity \
        == Decimal("1")
    hits_after = _hits(session, house)
    assert hits_after in (Decimal("17"), Decimal("14"))  # 3 a bite
    # a weak attacker still scratches: damage floors at 1 (scan ticks:
    # each tick's roll is deterministic, 95% hits land within a few)
    for t in range(3, 30):
        ev = combat.resolve_attack(session, house.id, wolf.id, t)
        if ev.get("hit"):
            break
    assert ev["hit"] is True and Decimal(ev["damage"]) == Decimal("1")
    # the house dies over repeated certain hits; loot lands on the wolf
    for t in range(40, 90):
        if session.get(Entity, house.id).status != EntityStatus.ACTIVE:
            break
        combat.resolve_attack(session, wolf.id, house.id, t)
    assert session.get(Entity, house.id).status != EntityStatus.ACTIVE
    assert markets.get_holding(session, wolf.id, "PELT").quantity \
        == Decimal("1")


def test_prowl_picks_the_noisiest_then_anyone(session):
    """A desperate prowl (attack with no target): the noisiest speaker
    of the recent night, else a random active individual."""
    wolf, house = _world(session)
    loud = create_entity(session, "Loud", EntityType.INDIVIDUAL)
    markets.adjust_holding(session, loud, "HITS", Decimal("20"))
    session.commit()
    # seed a recent say from the loud house (tick 1, delivered)
    from econengine import tick as tick_mod
    from econengine.models import Tick
    session.add(Tick(number=1, events=[
        {"type": "say", "entity_id": loud.id},
    ]))
    session.commit()
    assert combat.pick_prey(session, 2, exclude_id=wolf.id) == loud.id
    assert combat.pick_prey(session, 2, exclude_id=loud.id) != loud.id


def test_spawn_cadence_caps_and_templates(session):
    """Population as declared rules: from_round/every/up_to/max_alive,
    and the template materializes stats, holdings, account, script."""
    goods.create_good(session, "HITS")
    spawns.set_script_source(session, "beast", "-- prowl")
    spawns.set_rules(session, {
        "from_round": 5, "every_rounds": 5, "up_to": 3, "max_alive": 2,
        "name_prefix": "Beast",
        "template": {"entity_type": "individual",
                     "stats": {"ATTACK": 2},
                     "holdings": {"HITS": 5},
                     "script_setting": "beast",
                     "account": {"COIN": 0}},
    })
    session.commit()
    assert spawns.apply_on_round(session, 4) == []
    born = spawns.apply_on_round(session, 5)
    assert len(born) == 2                            # up_to 3, capped at 2
    beast = session.get(Entity, born[0]["entity_id"])
    assert combat.get_stats(session, beast.id) == {"ATTACK": Decimal("2")}
    assert markets.get_holding(session, beast.id, "HITS").quantity \
        == Decimal("5")
    assert spawns.apply_on_round(session, 10) == []  # at the cap already
    # ...until one dies: room again
    beast.status = EntityStatus.INCAPACITATED
    session.commit()
    assert len(spawns.apply_on_round(session, 15)) == 1
