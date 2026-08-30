"""Spawns — population as declared world rules.

Wildlife is not installed once at genesis and left to run down: packs
breed, monsters stir, the pressure renews. The SPAWN_RULES world
setting declares the cadence and the template; the platform's round
resolution calls ``apply_on_round`` after each round commits, and the
pass materializes what the rules call for — up to a cap, so a world
can be cleaned out between waves:

    {"from_round": 5, "every_rounds": 5, "up_to": 3, "max_alive": 4,
     "name_prefix": "Wolf Pack",
     "template": {"entity_type": "individual",
                  "stats": {"ATTACK": 4, "DEFENSE": 1, "HITS": 12},
                  "holdings": {"MEAT": 1, "PELT": 1},
                  "script_setting": "wolf.pack_source",
                  "account": {"COIN": 0}}}

The template is data all the way down: stats rows, holdings grants, a
COIN account, and a behaviour script read from its own world setting
(the pack installs the source there at genesis, gated like any other).
Spawning is the world's act, not any entity's: no caller, no
capability, no steering — rows describe creatures, the clock calls,
the pass creates.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import combat, services
from .models import Entity, EntityStatus, EntityType, Script, ScriptType, WorldSetting

SPAWN_RULES_KEY = "spawns.rules"
SCRIPT_SETTING_PREFIX = "spawns.script."


def set_rules(session: Session, rules: dict) -> None:
    row = session.get(WorldSetting, SPAWN_RULES_KEY)
    if row is None:
        session.add(WorldSetting(key=SPAWN_RULES_KEY, value=rules))
    else:
        row.value = rules


def get_rules(session: Session) -> dict | None:
    row = session.get(WorldSetting, SPAWN_RULES_KEY)
    return dict(row.value) if row is not None else None


def set_script_source(session: Session, key: str, source: str) -> None:
    """Install a template script source under the spawns namespace."""
    full = SCRIPT_SETTING_PREFIX + key
    row = session.get(WorldSetting, full)
    if row is None:
        session.add(WorldSetting(key=full, value=source))
    else:
        row.value = source


def get_script_source(session: Session, key: str) -> str | None:
    row = session.get(WorldSetting, SCRIPT_SETTING_PREFIX + key)
    return row.value if row is not None else None


def alive_count(session: Session, name_prefix: str) -> int:
    like = f"{name_prefix}%"
    return int(session.execute(
        select(func.count()).select_from(Entity).where(
            Entity.status == EntityStatus.ACTIVE,
            Entity.name.like(like),
        )
    ).scalar_one())


def spawn_one(session: Session, name: str, template: dict) -> Entity:
    """Materialize one creature from a template dict."""
    from . import markets  # deferred

    entity = services.create_entity(
        session, name, EntityType(str(template.get("entity_type", "individual"))))
    currency, balance = next(iter(
        (template.get("account") or {"COIN": 0}).items()))
    services.create_account(session, entity, currency,
                            initial_balance=Decimal(str(balance)))
    stats = {str(k).upper(): Decimal(str(v))
             for k, v in (template.get("stats") or {}).items()}
    for stat, value in sorted(stats.items()):
        combat.create_stat(session, entity.id, stat, value)
    holdings = dict(template.get("holdings") or {})
    if "HITS" in stats and not holdings.get("HITS"):
        # Health is assigned, not chosen: the innate HITS stat is the
        # body; the holding starts whole and only combat drains it.
        holdings["HITS"] = stats["HITS"]
    for symbol, qty in sorted(holdings.items()):
        markets.adjust_holding(session, entity, symbol, Decimal(str(qty)))
    source = get_script_source(session, template.get("script_setting", ""))
    if source:
        session.add(Script(
            name=f"{name.lower().replace(' ', '-')}-behaviour",
            source=source,
            script_type=ScriptType.BEHAVIOUR,
            entity_id=entity.id,
            timeout_ms=200,
            state={},
        ))
    session.flush()
    return entity


def apply_on_round(session: Session, round_no: int) -> list[dict]:
    """The clock's call after round ``round_no`` committed: spawn what
    the rules call for. Returns one spawn record per creature born."""
    rules = get_rules(session)
    if not rules:
        return []
    if round_no < int(rules.get("from_round", 1)):
        return []
    if (round_no - int(rules.get("from_round", 1))) \
            % int(rules.get("every_rounds", 1)) != 0:
        return []
    prefix = rules.get("name_prefix", "")
    alive = alive_count(session, prefix)
    room = int(rules.get("max_alive", 0)) - alive
    n = max(0, min(int(rules.get("up_to", 0)), room))
    born: list[dict] = []
    for i in range(n):
        creature = spawn_one(
            session, f"{prefix} {roman(alive + len(born) + i + 1)}",
            rules.get("template", {}))
        born.append({"name": creature.name, "entity_id": creature.id})
    if born:
        session.flush()
    return born


def roman(n: int) -> str:
    numerals = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    out = []
    for value, symbol in numerals:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out) or "I"
