"""Genesis setup for the "Fielding" inequality experiment economy.

Builds a synthetic economy directly on econengine (no HTTP layer, no FastAPI
-- this is the "economic modelling software" consumer design.md always
described, exercised for real): a population of Individuals with
heterogeneous starting conditions, a handful of land-owning Firms, a
Treasury that redistributes voluntary tax remittances, and the full
goods/needs/recipes/tech/parcels content that lets wealth concentration,
mobility, and inheritance policy emerge from mechanism rather than being
assumed.

Taxation is modeled as voluntary self-assessed remittance (individual.lua),
not forced extraction -- the engine's ownership invariant means a Government
script can never reach into another entity's account, so redistribution
only ever moves money out of the Treasury's own account (treasury.lua).
"""

import random
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from econengine import goods, markets, needs, parcels, production, services, tech
from econengine.models import Entity, EntityType, Script, ScriptType

_LUA_DIR = Path(__file__).parent / "lua"

_WEALTH_TIERS = [Decimal("100"), Decimal("500"), Decimal("2000")]
_WEALTH_WEIGHTS = [0.70, 0.20, 0.10]

_SOIL_QUANTITY = Decimal("50")
_SOIL_CAPACITY = Decimal("50")
_SOIL_REGEN = Decimal("2")


# Expected FOOD from one field-tick under FARM_FOOD_HAND, from the branch
# table in _create_recipes below.
_FOOD_PER_FIELD_TICK = 0.70 * 6 + 0.25 * 3  # 4.95
_SUBSISTENCE_FOOD = 0.8                     # HUNGER quantity_per_tick

# How much food the land can grow per head, as a multiple of bare
# subsistence. Calibrated by sweep at 30 individuals over 100 ticks:
#
#   fields  food price      food sold   mean hunger   incapacitated
#        7    0.94 -> 1.19       1.00          0.69          0 -> 11
#        9    1.69 -> 2.62       0.69          0.86          0 ->  0
#       11    1.79 -> 2.85       0.86          1.00          0 ->  0
#       13    0.57 -> 0.61       0.70          1.00          0 ->  0
#
# Seven fields is a death spiral, not an economy: unmet hunger applies
# COND-WEAK, which halves labor productivity, which cuts output further.
# Thirteen is the opposite failure and a subtler one -- so much food goes
# unsold that a wide band of prices all clear the same volume, and the
# auction's tie-break (nearest the last price) simply freezes the level
# wherever the opening ticks happened to put it. That is what the old
# calibration was doing, and it is why doubling every starting balance used
# to move prices DOWN: with no scarcity there is nothing for the price level
# to be determined BY. Nine keeps the food market genuinely tight -- most of
# what is offered sells, prices move, scarcity bites at the margin without
# taking the whole population with it.
_FIELDS_PER_SUBSISTENCE = 1.85


def recommended_n_firms(n_individuals: int, smallholder_fraction: float = 0.15) -> int:
    """Firms are sized by the land the economy needs, not by a flat number.

    A fixed employer count regardless of population starves the labor market
    of buyers as population grows; sizing it by landless headcount instead
    (the previous fix) overshot the other way and buried the economy in
    surplus food. What actually has to scale with population is FARMLAND,
    since every field is an employer and every mouth needs feeding -- so
    derive total fields from the population's food requirement and give the
    firms whatever the smallholders don't already own."""
    fields = round(
        n_individuals * _SUBSISTENCE_FOOD * _FIELDS_PER_SUBSISTENCE / _FOOD_PER_FIELD_TICK
    )
    smallholders = round(n_individuals * smallholder_fraction)
    return max(1, fields - smallholders)


@dataclass
class ScenarioConfig:
    n_individuals: int = 30
    # None means "derive it from the population" -- a hardcoded second copy
    # of a default is what silently undid several rounds of calibration once
    # already (see NOTES.md, bug 5), and a firm count that does not track
    # population is exactly the thing recommended_n_firms exists to prevent.
    n_firms: int | None = None
    tax_rate: Decimal = Decimal("0")
    tax_threshold: Decimal = Decimal("0")
    estate_rule: str = "burn"  # burn | treasury | heir
    redistribution_period: int = 5
    smallholder_fraction: float = 0.15
    seed: int = 0


@dataclass
class Scenario:
    config: ScenarioConfig
    treasury_id: str
    treasury_account_id: str
    individual_ids: list[str]
    firm_ids: list[str]
    starting_balance: dict[str, Decimal] = field(default_factory=dict)
    starting_skill: dict[str, Decimal] = field(default_factory=dict)
    landed: dict[str, bool] = field(default_factory=dict)


def _read_lua(name: str) -> str:
    return (_LUA_DIR / name).read_text()


def _read_behaviour_lua(name: str) -> str:
    """Behaviour scripts are prelude + script. Scripts live in the DB as flat
    source strings and the sandbox has no `require`, so the shared pricing
    machinery is prepended rather than imported -- which means Lua error line
    numbers for a behaviour script are offset by the prelude's length."""
    return _read_lua("prelude.lua") + "\n" + _read_lua(name)


def build_economy(session: Session, config: ScenarioConfig) -> Scenario:
    rng = random.Random(config.seed)

    # Resolve it here rather than defaulting it at every call site, and write
    # it back so the number that ran is the number that gets recorded in the
    # result JSON.
    if config.n_firms is None:
        config.n_firms = recommended_n_firms(config.n_individuals, config.smallholder_fraction)

    _create_goods(session)
    _create_needs(session)
    _create_tech(session)
    _create_markets(session)
    _create_recipes(session)

    bank = services.create_entity(session, "Central Bank", EntityType.BANK)
    bank.is_monetary_authority = True
    services.create_account(session, bank, "USD")

    treasury = services.create_entity(session, "Treasury", EntityType.GOVERNMENT)
    treasury_account = services.create_account(session, treasury, "USD")

    firm_ids = _create_firms(session, config)
    individual_ids, starting_balance, starting_skill, landed = _create_individuals(
        session, config, rng
    )

    if config.estate_rule == "heir":
        _assign_heirs(session, individual_ids, rng)

    _wire_scripts(session, config, treasury, treasury_account, individual_ids, firm_ids, rng)

    session.flush()
    return Scenario(
        config=config,
        treasury_id=treasury.id,
        treasury_account_id=treasury_account.id,
        individual_ids=individual_ids,
        firm_ids=firm_ids,
        starting_balance=starting_balance,
        starting_skill=starting_skill,
        landed=landed,
    )


def _create_goods(session: Session) -> None:
    # decay_per_tick < 1 (not a full wipe): a script only ever sees holdings
    # from BEFORE this tick's auction, so a firm's just-bought LABOR would be
    # destroyed by decay before it could ever act on it if decay were 100%
    # (decay runs after the auction, before the next tick's scripts). A
    # partial decay still discourages hoarding while letting a market-bought
    # unit survive long enough for its buyer's next script to use it.
    goods.create_good(
        session, "LABOR", decay_per_tick=Decimal("0.5"),
        auto_issue_quantity=Decimal("1"), auto_issue_entity_type=EntityType.INDIVIDUAL,
    )
    goods.create_good(session, "LABOR-FARM", decay_per_tick=Decimal("0.5"))
    goods.create_good(session, "SKILL-FARM", decay_per_tick=Decimal("0.02"))
    goods.create_good(session, "FOOD", decay_per_tick=Decimal("0.3"))
    goods.create_good(session, "CLOTHES", decay_per_tick=Decimal("0.05"))
    goods.create_good(session, "TOOLS")
    goods.create_good(
        session, "COND-WEAK",
        modifies_pattern="LABOR*", modifies_factor=Decimal("0.7"),
        incapacitates_at=Decimal("30"),
    )


def _create_needs(session: Session) -> None:
    needs.create_need(
        session, "HUNGER", Decimal("0.8"), ["FOOD"],
        entity_type=EntityType.INDIVIDUAL, priority=0,
        condition_symbol="COND-WEAK", condition_quantity=Decimal("1"),
    )
    needs.create_need(
        session, "COMFORT", Decimal("0.2"), ["CLOTHES"],
        entity_type=EntityType.INDIVIDUAL, priority=1,
    )


def _create_tech(session: Session) -> None:
    tech.create_technology(session, "AGRONOMY")


def _create_markets(session: Session) -> None:
    for symbol in ("LABOR", "LABOR-FARM", "FOOD", "CLOTHES", "TOOLS"):
        markets.create_market(session, symbol, "USD")


def _create_recipes(session: Session) -> None:
    production.create_recipe(
        session, "WORK_AS_FARMER",
        inputs={"LABOR": Decimal("1")}, outputs={"LABOR-FARM": Decimal("1")},
        duration_ticks=0, good_requirements={"SKILL-FARM": Decimal("1")},
    )
    production.create_recipe(
        session, "FARM_FOOD_HAND",
        inputs={"LABOR-FARM": Decimal("1")}, outputs={},
        duration_ticks=1, requires_facility="FARM",
        deposit_inputs={"SOIL-FERTILITY": Decimal("1")},
        branches=[
            {"weight": Decimal("0.70"), "outputs": {"FOOD": Decimal("6"), "SKILL-FARM": Decimal("0.05")}},
            {"weight": Decimal("0.25"), "outputs": {"FOOD": Decimal("3")}, "label": "mediocre harvest"},
            {"weight": Decimal("0.05"), "outputs": {}, "label": "crop failure"},
        ],
    )
    production.create_recipe(
        session, "FARM_FOOD_TOOLED",
        inputs={"LABOR-FARM": Decimal("1"), "TOOLS": Decimal("0.02")}, outputs={},
        duration_ticks=1, requires_facility="FARM", requires=["AGRONOMY"],
        deposit_inputs={"SOIL-FERTILITY": Decimal("1")},
        branches=[
            {"weight": Decimal("0.80"), "outputs": {"FOOD": Decimal("10"), "SKILL-FARM": Decimal("0.05")}},
            {"weight": Decimal("0.15"), "outputs": {"FOOD": Decimal("5")}, "label": "mediocre harvest"},
            {"weight": Decimal("0.05"), "outputs": {}, "label": "crop failure"},
        ],
    )
    production.create_recipe(
        session, "CRAFT_TOOLS",
        inputs={"LABOR": Decimal("3")}, outputs={"TOOLS": Decimal("1")}, duration_ticks=2,
    )
    production.create_recipe(
        session, "MAKE_CLOTHES",
        inputs={"LABOR": Decimal("2")}, outputs={"CLOTHES": Decimal("3")}, duration_ticks=1,
    )
    production.create_recipe(
        session, "RESEARCH_AGRONOMY",
        inputs={"LABOR-FARM": Decimal("5")}, outputs={},
        duration_ticks=5, unlocks=["AGRONOMY"],
    )


def _grant_field(session: Session, owner: Entity) -> None:
    parcel = parcels.create_parcel(session, "FIELD", name=f"{owner.name}'s Field", owner=owner)
    parcels.add_facility(session, parcel, "FARM", built_tick=None)
    parcels.add_deposit(
        session, parcel, "SOIL-FERTILITY", _SOIL_QUANTITY,
        capacity=_SOIL_CAPACITY, regen_per_tick=_SOIL_REGEN,
    )


def _create_firms(session: Session, config: ScenarioConfig) -> list[str]:
    firm_ids = []
    for i in range(config.n_firms):
        firm = services.create_entity(session, f"Firm {i + 1}", EntityType.BUSINESS)
        services.create_account(session, firm, "USD", initial_balance=Decimal("3000"))
        _grant_field(session, firm)
        # Standing agronomist -- what lets a firm self-convert bought raw
        # LABOR into LABOR-FARM instead of depending on a thin market for it.
        # Headroom above the good_requirements threshold (>= 1): SKILL-FARM
        # decays every tick regardless of use, and buying labor takes at
        # least a tick to arrive, so starting at exactly 1.0 decays below
        # the threshold before a firm ever gets its first chance to use it
        # -- a permanent bootstrap deadlock, not a real outcome.
        markets.adjust_holding(session, firm, "SKILL-FARM", Decimal("2"))
        firm_ids.append(firm.id)
    return firm_ids


def _create_individuals(
    session: Session, config: ScenarioConfig, rng: random.Random
) -> tuple[list[str], dict[str, Decimal], dict[str, Decimal], dict[str, bool]]:
    individual_ids: list[str] = []
    starting_balance: dict[str, Decimal] = {}
    starting_skill: dict[str, Decimal] = {}
    landed: dict[str, bool] = {}

    n_smallholders = round(config.n_individuals * config.smallholder_fraction)
    smallholder_indices = set(rng.sample(range(config.n_individuals), n_smallholders))

    for i in range(config.n_individuals):
        base = rng.choices(_WEALTH_TIERS, weights=_WEALTH_WEIGHTS)[0]
        noise = Decimal(str(round(rng.uniform(0.8, 1.2), 4)))
        balance = (base * noise).quantize(Decimal("0.0001"))

        person = services.create_entity(session, f"Person {i + 1}", EntityType.INDIVIDUAL)
        services.create_account(session, person, "USD", initial_balance=balance)
        # A small pantry buffer: production takes a tick or more to ramp up
        # (auto-issued labor -> converted -> farmed -> sold -> bought is at
        # best a 2-tick round trip), so without a buffer every single
        # individual would hit 0% hunger satisfaction on tick 1 regardless of
        # policy, instantly and universally triggering the poverty condition
        # before the economy has even started -- a bootstrap artifact, not a
        # real outcome. This buys a few ticks for trade to actually begin.
        markets.adjust_holding(session, person, "FOOD", Decimal("8"))

        is_smallholder = i in smallholder_indices
        if is_smallholder:
            _grant_field(session, person)
            markets.adjust_holding(session, person, "SKILL-FARM", Decimal("2"))
            starting_skill[person.id] = Decimal("2")
        else:
            starting_skill[person.id] = Decimal("0")

        individual_ids.append(person.id)
        starting_balance[person.id] = balance
        landed[person.id] = is_smallholder

    return individual_ids, starting_balance, starting_skill, landed


def _assign_heirs(session: Session, individual_ids: list[str], rng: random.Random) -> None:
    shuffled = individual_ids[:]
    rng.shuffle(shuffled)
    n = len(shuffled)
    for i, entity_id in enumerate(shuffled):
        entity = session.get(Entity, entity_id)
        entity.heir_id = shuffled[(i + 1) % n]


def _wire_scripts(
    session: Session,
    config: ScenarioConfig,
    treasury: Entity,
    treasury_account,
    individual_ids: list[str],
    firm_ids: list[str],
    rng: random.Random,
) -> None:
    individual_accounts = []
    for entity_id in individual_ids:
        entity = session.get(Entity, entity_id)
        individual_accounts.append(entity.accounts[0].id)

    individual_source = _read_behaviour_lua("individual.lua")
    firm_source = _read_behaviour_lua("firm.lua")
    treasury_source = _read_lua("treasury.lua")

    for entity_id in individual_ids:
        session.add(Script(
            name=f"individual-behaviour-{entity_id}",
            script_type=ScriptType.BEHAVIOUR,
            source=individual_source,
            entity_id=entity_id,
            timeout_ms=200,
            state={
                "tax_rate": str(config.tax_rate),
                "tax_threshold": str(config.tax_threshold),
                "treasury_account_id": treasury_account.id,
            },
        ))

    for entity_id in firm_ids:
        session.add(Script(
            name=f"firm-behaviour-{entity_id}",
            script_type=ScriptType.BEHAVIOUR,
            source=firm_source,
            entity_id=entity_id,
            timeout_ms=200,
            state={
                # Firms bid labor at its marginal revenue product (firm.lua),
                # which every firm computes identically -- so without a little
                # idiosyncrasy they all quote the same number and the auction
                # rations them by order creation time, handing the same firm
                # the scarce labor every tick forever. A spread of bid
                # aggressiveness is also just true of real firms.
                "bid_factor": round(rng.uniform(0.9, 1.1), 4),
                # Staggered so the research pushes (and the labor demand
                # spike each one brings) don't all land on the same tick.
                "research_timer": rng.randrange(20),
            },
        ))

    session.add(Script(
        name="treasury-policy",
        script_type=ScriptType.POLICY,
        source=treasury_source,
        entity_id=treasury.id,
        timeout_ms=500,
        state={
            "recipients": individual_accounts,
            "redistribution_period": config.redistribution_period,
            "counter": 0,
        },
    ))
