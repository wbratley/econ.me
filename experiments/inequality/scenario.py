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
from decimal import Decimal, ROUND_DOWN
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

# A fuel seam regenerates more slowly than soil and holds less, so a plant run
# flat out outruns its own land and has to idle -- energy is the sector where
# land quality bites hardest. GENERATE_POWER draws 1 per tick against a regen
# of 1.2, so a single plant is sustainable and a second on the same parcel
# would not be.
_FUEL_QUANTITY = Decimal("30")
_FUEL_CAPACITY = Decimal("30")
_FUEL_REGEN = Decimal("1.2")


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
    # Bare parcels each firm holds on top of its working farm. This is the
    # land the build mechanic gets to allocate: raise it and firms have room
    # to over-build one sector, drop it to 0 and the economy reverts to
    # farming only, which is every result recorded before rent and bills.
    bare_land_per_firm: int = 2

    # --- The household nominal anchor (swept by calibrate.py) -------------
    # A household commits `balance / planning_horizon` per tick and splits it
    # across goods by these shares. The horizon is what pins the price level
    # (see bug 8), so it cannot be widened casually -- but it was calibrated
    # against a basket of food alone, and rent and power tripled the real flow
    # it has to fund.
    planning_horizon: float = 20.0
    food_budget_share: float = 0.5
    shelter_budget_share: float = 0.20
    energy_budget_share: float = 0.10
    clothes_budget_share: float = 0.15

    # --- The extensive margin of labour supply ------------------------------
    # Ticks of ordinary living (the basket at market prices) a household's
    # balance must already cover before it stops offering wage labour at all.
    # Work is a disutility: you sell your labour because you cannot pay for the
    # week otherwise, and whoever can live off what they own does not turn up.
    # Below this the reservation wage still does its work on the intensive
    # margin. Set very high to recover the old always-participate behaviour.
    #
    # 40 is a deliberately inert default, and that is a statement about the
    # economy rather than about the number. Measured over individuals only,
    # smoothed (30 individuals, seed 0), cover peaks at 36.8 for the RICHEST
    # person at t30 and decays to 11.9 by t150; the median goes 10.3 -> 0.39.
    # So nobody in this economy can live off what they own, at any point, and
    # the extensive margin never binds. It fires when it is given something to
    # bite on -- at a threshold of 5, two workers withdraw at t15 with all 30
    # still alive -- but a threshold tuned to bind on destitution would be
    # fitting to the very brokenness the calibration work is trying to remove.
    # Left where it belongs so it becomes live once households can accumulate.
    work_free_cover: float = 40.0

    # --- Minimum wage ------------------------------------------------------
    # A floor on the LABOR clearing price, implemented supply-side (workers
    # refuse to sell below it; individual.lua). 0 = off. Tested because the
    # labour market runs with persistent excess demand (firms order ~65 units
    # against ~12 supplied), so the wage is pinned by worker reservation,
    # below firm marginal product -- firms keep the gap as rent and the wage
    # does not do its job as the household-income return channel. Because
    # employment is supply-capped (the condition stack), a moderate floor
    # transfers that rent to workers without shedding jobs; a floor above the
    # farm marginal product starts to shed farm employment instead.
    min_wage: Decimal = Decimal("0")

    seed: int = 0

    # --- Firm profit margin ------------------------------------------------
    # Gross margin a firm requires on revenue, withheld from its labor bid and
    # added back to its ask (firm.lua applies the two symmetrically, which is
    # what keeps the price level from drifting -- see the note there).
    #
    # 0.20 chosen on evidence, not taste: at n=30 it is the lowest margin
    # swept that holds all five firms solvent through tick 150, where margin 0
    # has already lost two firms and 82% of the sector's capital, and 0.10
    # holds 4.57. See NOTES.md "Firms with a margin".
    #
    # It is NOT chosen for its mortality effect, which is small (14.10 deaths
    # against 15.53 at margin 0, n=30, t400). It is chosen because a firm
    # sector that is still trading at the end of a run is the precondition for
    # reading anything else off a long run at all -- at margin 0 most of a
    # 400-tick run happens in a post-bankruptcy economy of one or two
    # survivors, which is a confound in every arm comparison.
    #
    # WARNING for anyone reproducing older numbers: every result in NOTES.md
    # above the "arm matrix at margin 0.20" section was measured at 0, and
    # needs firm_margin=Decimal("0") passed explicitly to reproduce.
    firm_margin: Decimal = Decimal("0.20")

    # --- Capital ownership (SHARE-FIRM-n) ---------------------------------
    # Without these the model has no capital-income channel at all: an
    # individual's only income is wages, so the main driver of real wealth
    # concentration is simply absent. "none" is the default so every existing
    # result reproduces unchanged -- allocation must make no uuid4 draws when
    # off, since the ID stream seeds the outcome rolls (see determinism.py).
    share_allocation: str = "none"          # none | wealth | equal
    shares_per_firm: Decimal = Decimal("100")
    # Firms may only ever distribute cash ABOVE their genesis endowment, i.e.
    # real accumulated profit and never working capital. This is not a
    # nicety: at firm_margin 0 firms *decapitalise*, so every friction
    # (0.3/tick FOOD decay, 5% crop failure, concede() cutting asks below
    # cost) is a pure loss and a payout rule with a lower reserve would simply
    # speed the bankruptcies up. At the current 0.20 default firms hold their
    # capital far longer, so this reserve now binds much less often and
    # dividends start flowing earlier in a run -- which is the intended
    # behaviour, but it does mean the share arms are not comparable to the
    # ones recorded before the margin existed.
    firm_cash_reserve: Decimal = Decimal("3000")
    dividend_period: int = 10
    dividend_payout: Decimal = Decimal("0.5")   # fraction of profit paid per period


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
    starting_shares: dict[str, Decimal] = field(default_factory=dict)


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

    # firm.lua clamps an out-of-range margin back to 0 rather than dividing by
    # zero, which would silently run the baseline while the result JSON claimed
    # otherwise. Fail here instead.
    if not (0 <= config.firm_margin < 1):
        raise ValueError(
            f"firm_margin must be in [0, 1), got {config.firm_margin}"
        )

    _create_goods(session)
    _create_needs(session)
    _create_tech(session)
    _create_markets(session, config)
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

    # After heirs, before scripts: allocation reads starting_balance and makes
    # no rng draws, so with share_allocation="none" nothing above or below
    # this line moves and every pre-shares result reproduces exactly.
    starting_shares = _allocate_shares(session, config, individual_ids, starting_balance)

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
        starting_shares=starting_shares,
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
    # COND-WEAK recovers. It did not until now, and that was the single
    # biggest distortion in this experiment: with no decay it is a LIFETIME
    # counter of missed meals, so at tick resolution every hungry spell in a
    # run is exactly one tick long (532 of them, none of length two) and
    # people die of thirty non-consecutive bad days while solvent, bidding
    # successfully, and surrounded by a 60-100% food surplus.
    #
    # conditions.py states the contract: proportional decay against a
    # constant grant converges to grant/decay, and incapacitates_at must sit
    # BELOW that equilibrium or it never fires. The grant is +1 per hungry
    # tick, so with decay d an entity hungry a fraction f of the time settles
    # at f/d, and dies iff f > 30d. At d = 0.02:
    #
    #   f = 1.00 (true famine)     equilibrium 50  -> dies after ~46 ticks
    #   f > 0.60 (hungry most days) equilibrium >30 -> dies eventually
    #   f = 0.134 (what was measured) equilibrium 6.7 -> survives, but stays
    #                                 above 1, so keeps the labour penalty
    #
    # That is the intended shape: chronic hunger throttles you and a real
    # famine still kills, but an intermittent miss no longer accumulates into
    # a death sentence.
    goods.create_good(
        session, "COND-WEAK",
        modifies_pattern="LABOR*", modifies_factor=Decimal("0.7"),
        incapacitates_at=Decimal("30"),
        decay_per_tick=Decimal("0.02"),
    )

    # --- Rent and bills ----------------------------------------------------
    #
    # SHELTER and ENERGY decay COMPLETELY each tick, which is the whole trick
    # that makes a recurring obligation work without a new engine mechanism.
    # Consumption runs after the auction and before decay, so buying exactly
    # this tick's requirement satisfies the need and nothing carries over: you
    # are buying one tick's occupancy and one tick's power, and next tick the
    # bill is due again. Nobody can stockpile a year of rent in a good week.
    #
    # It also sidesteps the ownership invariant the same way taxation does. A
    # landlord cannot reach into a tenant's account any more than a treasury
    # can (design.md § military, and see the taxation note above) -- so rent
    # is not taken, it is *bought*, and the consequence of not paying is
    # simply that the need goes unmet. No forced transfer, no seizure, no new
    # primitive. Eviction is the absence of a purchase.
    goods.create_good(session, "SHELTER", decay_per_tick=Decimal("1"))
    goods.create_good(session, "ENERGY", decay_per_tick=Decimal("1"))

    # Both carry decay FROM THE START -- the COND-WEAK artifact above is what
    # happens when a damage counter has no way down.
    #
    # They deliberately bite on DIFFERENT margins rather than being one effect
    # at two strengths, and what makes that possible is where the engine reads
    # a condition's modifier. There are exactly two such sites (goods.py,
    # production.py): the auto-issue top-up target, and a recipe's input and
    # good_requirement checks. So a condition can throttle what you are ISSUED
    # and what you are ABLE TO DO -- it cannot reach consumption, orders or
    # cash. Within that, the two needs express two different kinds of harm.
    #
    # NO HEATING is the intensive margin: you are cold, so you get less done.
    # It scales "LABOR" alone, which is the auto-issued good, so it cuts the
    # hours you have to sell and nothing else. Unpleasant, survivable,
    # recoverable the moment you can pay the bill again.
    goods.create_good(
        session, "COND-COLD",
        modifies_pattern="LABOR", modifies_factor=Decimal("0.80"),
        decay_per_tick=Decimal("0.02"),
    )
    # ROUGH SLEEPING is the extensive margin, and the pattern is "*" on
    # purpose. It scales every symbol at both read sites, so it cuts the labour
    # you are issued AND every recipe input and good_requirement you try to
    # meet -- most sharply `SKILL-FARM`, which `WORK_AS_FARMER` gates on at >=
    # 1. A smallholder sleeping rough stops being able to work their own land;
    # a labourer's day shrinks. Losing your home does not just cost you a
    # slice of your wage, it locks you out of skilled and self-provisioning
    # work, which is the difference between a bad month and a trap: less
    # labour -> less income -> still cannot pay rent.
    #
    # It is also the one new route to incapacity, and the arithmetic is the
    # point rather than an afterthought. Grant is 1 x (1 - satisfaction) per
    # tick against decay 0.02, so an entity unhoused a fraction f of the time
    # settles at f/0.02 = 50f, and the threshold only ever fires if
    # 50f > incapacitates_at. At 40:
    #
    #   f = 1.00 (never housed)      equilibrium 50 -> crosses 40 at ~80 ticks
    #   f = 0.80 (housed one tick in five) equilibrium 40 -> only just, slowly
    #   f = 0.50 (housed half the time)    equilibrium 25 -> never
    #
    # That is the property COND-WEAK lacked: it is reachable only under
    # sustained near-total destitution, never by intermittently missing rent.
    # In the current uncalibrated economy shelter satisfaction sits near 0.5,
    # so this fires for nobody -- it is a tail, deliberately, not a default.
    goods.create_good(
        session, "COND-EXPOSED",
        modifies_pattern="*", modifies_factor=Decimal("0.70"),
        incapacitates_at=Decimal("40"),
        decay_per_tick=Decimal("0.02"),
    )


def _create_needs(session: Session) -> None:
    # Priority is the order the consumption pass draws in, and here it is also
    # the order a household goes under: food before rent before power before
    # clothes. That ordering is the mechanism by which a squeeze shows up as
    # cold and crowded rather than as starvation -- the essential need is
    # served first, so the bills are what get missed. Making it explicit
    # matters because it is a claim about behaviour, not a tie-break.
    needs.create_need(
        session, "HUNGER", Decimal("0.8"), ["FOOD"],
        entity_type=EntityType.INDIVIDUAL, priority=0,
        condition_symbol="COND-WEAK", condition_quantity=Decimal("1"),
    )
    needs.create_need(
        session, "SHELTER", Decimal("1"), ["SHELTER"],
        entity_type=EntityType.INDIVIDUAL, priority=1,
        condition_symbol="COND-EXPOSED", condition_quantity=Decimal("1"),
    )
    needs.create_need(
        session, "POWER", Decimal("1"), ["ENERGY"],
        entity_type=EntityType.INDIVIDUAL, priority=2,
        condition_symbol="COND-COLD", condition_quantity=Decimal("1"),
    )
    needs.create_need(
        session, "COMFORT", Decimal("0.2"), ["CLOTHES"],
        entity_type=EntityType.INDIVIDUAL, priority=3,
    )


def _create_tech(session: Session) -> None:
    tech.create_technology(session, "AGRONOMY")


def share_symbol(firm_index: int) -> str:
    """SHARE-FIRM-1, SHARE-FIRM-2, ... A share needs no Good row: bare symbols
    work everywhere, and the defaults a Good would supply (no decay, no
    auto-issue) are exactly what a share wants."""
    return f"SHARE-FIRM-{firm_index + 1}"


def _create_markets(session: Session, config: ScenarioConfig) -> None:
    for symbol in ("LABOR", "LABOR-FARM", "FOOD", "CLOTHES", "TOOLS",
                    "SHELTER", "ENERGY"):
        markets.create_market(session, symbol, "USD")
    if config.share_allocation != "none":
        # Shares are tradable in principle from the start. No script places
        # share orders yet, so these markets sit idle -- but the register
        # (ctx.query.holders) reads live holdings, so a firm's dividend
        # follows the shares the moment anything does trade them.
        for i in range(config.n_firms):
            markets.create_market(session, share_symbol(i), "USD")


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
        # Tooled farming is a pure yield boost over hand farming (8.75 vs 4.95
        # expected FOOD per LABOR-FARM), gated only on the AGRONOMY unlock.
        # It previously also burned 0.02 TOOLS per farm-tick, which made
        # AGRONOMY a trap rather than a gain in this labour-starved economy:
        # tools are made by CRAFT_TOOLS (3 LABOR each), so unlocking the tech
        # switched every farm to a recipe whose capital input the economy
        # could not supply -- all tooled-farm attempts failed for want of
        # TOOLS, food output collapsed, and the population starved. The tool
        # requirement is removed so that researching AGRONOMY buys the real
        # labour-productivity gain it represents. (TOOLS still gate the
        # BUILD_DWELLING / BUILD_POWER_PLANT recipes below, so CRAFT_TOOLS and
        # the tool stock retain a purpose.)
        inputs={"LABOR-FARM": Decimal("1")}, outputs={},
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
        # Paid as a flow of raw LABOR, 1 per tick for 5 ticks, funded exactly
        # like a build: the firm idles one field (skips its WORK_AS_FARMER
        # conversion) while a research process runs, so the raw LABOR that
        # field would have absorbed survives to the per-tick draw at step 7c.
        # It used to draw LABOR-FARM and skip a HARVEST instead, but that was
        # fragile: LABOR-FARM is fungible, so when utilities or a labour
        # shortfall left fewer conversions succeeding than fields queued for
        # harvest, those harvests consumed the research's LABOR-FARM before
        # the draw -- 38/38 attempts failed "LABOR-FARM short" once firms
        # held five fields. Raw LABOR funded by a skipped conversion has no
        # such leak (it is the mechanism builds use, which complete reliably).
        inputs={}, outputs={},
        duration_ticks=5, unlocks=["AGRONOMY"],
        per_tick_inputs={"LABOR": Decimal("1")},
    )

    # --- Housing and power: the two other things land can be ---------------
    #
    # These are what make land contested rather than merely required. A parcel
    # is bare until something is built on it, and the three BUILD_ recipes are
    # mutually exclusive uses of the same acre -- a dwelling is a field that is
    # not growing food. Because `builds_facility` erects on the bound parcel at
    # completion and `ctx.parcels` reports each parcel's facilities, a firm can
    # see which of its land is empty, price the three uses off their own
    # output, and put up whichever pays best. Land allocation becomes something
    # the run produces, not something genesis decides.
    #
    # One-use-per-parcel is enforced in firm.lua (build only where
    # `#facilities == 0`), NOT by the engine, which is happy to stack a farm
    # and a dwelling on one parcel. That is the right split -- zoning is a
    # policy question and belongs in behaviour, not in mechanism -- but it does
    # mean the invariant is only as good as the script.
    production.create_recipe(
        session, "BUILD_FARM",
        # Paid as a flow over the construction period (per_tick_inputs) rather
        # than a 2-LABOR lump up front. The lump form was unreachable for the
        # same reason RESEARCH_AGRONOMY's was: LABOR decays 0.5/tick and farming
        # spends the firm's inflow as it arrives, so no 2-unit lump ever
        # accumulated -- builds stalled after tick 3 and left land bare for the
        # whole run. The flow form needs only ~0.67 LABOR held per tick, which
        # the firm supplies by idling one field while building (firm.lua leaves
        # one WORK_AS_FARMER conversion un-run so that raw LABOR survives to
        # the step-7c draw). Total labour is unchanged: 2/3 per tick x 3.
        inputs={}, outputs={},
        per_tick_inputs={"LABOR": Decimal("0.666667")},
        duration_ticks=3, builds_facility="FARM",
    )
    production.create_recipe(
        session, "BUILD_DWELLING",
        inputs={"LABOR": Decimal("4"), "TOOLS": Decimal("0.5")}, outputs={},
        duration_ticks=5, builds_facility="DWELLING",
    )
    production.create_recipe(
        session, "BUILD_POWER_PLANT",
        inputs={"LABOR": Decimal("6"), "TOOLS": Decimal("1")}, outputs={},
        duration_ticks=8, builds_facility="POWER-PLANT",
    )

    # Letting takes upkeep, not construction: a dwelling already standing
    # produces occupancy for several households a tick against a little
    # maintenance labour. Facility capacity does the rest -- one DWELLING backs
    # one running LET_DWELLING, so housing more people means building more.
    # Yields are set so one parcel serves about the same number of people
    # whatever is built on it: a field feeds 4.95/0.8 = 6.2 mouths a tick, so a
    # dwelling houses 6 and a plant powers 6. Without that parity the choice
    # between uses would be decided by an accident of units rather than by
    # prices, and "which use pays best" is the question the build mechanic
    # exists to ask.
    #
    # Labour intensity is NOT at parity, and deliberately so. Housing and power
    # are utilities: the cost is in the building, and running one afterwards
    # takes a caretaker, not a workforce. Giving them farm-like ongoing labour
    # (0.5 and 1.0) was an unexamined default, and the calibration sweep showed
    # what it cost -- running the genesis stock of 10 farms, 5 dwellings and 5
    # plants wanted 10x1 + 5x0.5 + 5x1 = 17.5 LABOR a tick against the ~11.8
    # the market actually clears, so most of every sector sat idle and no
    # budget share could fix it. At 0.2 and 0.3 the same stock wants 12.5,
    # which is within reach of what clears and leaves the margin to fund
    # clothes, tools and building out of the same labour pool.
    production.create_recipe(
        session, "LET_DWELLING",
        inputs={"LABOR": Decimal("0.2")}, outputs={"SHELTER": Decimal("6")},
        duration_ticks=1, requires_facility="DWELLING",
    )
    # Power draws a regenerating seam, so an energy plot has land *quality*
    # the way a field has soil, and generating flat out drains it faster than
    # it comes back.
    production.create_recipe(
        session, "GENERATE_POWER",
        inputs={"LABOR": Decimal("0.3")}, outputs={"ENERGY": Decimal("6")},
        duration_ticks=1, requires_facility="POWER-PLANT",
        deposit_inputs={"FUEL-SEAM": Decimal("1")},
    )


def _grant_field(
    session: Session, owner: Entity, name: str = "", built: bool = True,
    facility: str = "FARM",
) -> None:
    """One parcel of land, optionally with a farm already standing on it.

    Every parcel carries BOTH deposits regardless of what gets built, because
    the point of the fixed land pool is that a given acre could have been any
    of the three things. Endowing only the parcels destined to farm with soil
    would decide the allocation at genesis under another name, and the
    allocation is meant to be the run's output.

    `built=False` hands over bare land: no facility, so a firm has to choose a
    use and pay to build it before that acre produces anything at all.
    """
    parcel = parcels.create_parcel(
        session, "LAND", name=name or f"{owner.name}'s Land", owner=owner)
    if built:
        parcels.add_facility(session, parcel, facility, built_tick=None)
    parcels.add_deposit(
        session, parcel, "SOIL-FERTILITY", _SOIL_QUANTITY,
        capacity=_SOIL_CAPACITY, regen_per_tick=_SOIL_REGEN,
    )
    parcels.add_deposit(
        session, parcel, "FUEL-SEAM", _FUEL_QUANTITY,
        capacity=_FUEL_CAPACITY, regen_per_tick=_FUEL_REGEN,
    )


def _create_firms(session: Session, config: ScenarioConfig) -> list[str]:
    firm_ids = []
    for i in range(config.n_firms):
        firm = services.create_entity(session, f"Firm {i + 1}", EntityType.BUSINESS)
        services.create_account(session, firm, "USD", initial_balance=Decimal("3000"))
        # A working farm, a dwelling and a power plant each, plus bare land.
        #
        # The housing and power stock has to EXIST at genesis, not be built
        # from nothing. Measured without it: no firm can assemble the 4-6 LABOR
        # a build needs before the whole population has failed shelter and
        # power for the entire bootstrap, and since both conditions cut labour
        # productivity on top of COND-WEAK, the food economy goes with them --
        # food price 3 -> 111, hunger satisfaction 0.03, and not one dwelling
        # ever built. Real economies start with a housing stock; this one has
        # to as well. At 6 served per parcel, one of each per firm covers the
        # population exactly, so the build mechanic operates at the MARGIN --
        # which is where a build-or-not decision is interesting anyway.
        _grant_field(session, firm, name=f"{firm.name}'s Field")
        _grant_field(session, firm, name=f"{firm.name}'s Houses", facility="DWELLING")
        _grant_field(session, firm, name=f"{firm.name}'s Works", facility="POWER-PLANT")
        for j in range(config.bare_land_per_firm):
            _grant_field(session, firm, name=f"{firm.name}'s Plot {j + 1}", built=False)
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


def _allocate_shares(
    session: Session,
    config: ScenarioConfig,
    individual_ids: list[str],
    starting_balance: dict[str, Decimal],
) -> dict[str, Decimal]:
    """Hand out each firm's shares to the population. Returns per-person total
    shares held, for the metrics baseline.

    The two live modes are the experiment: "wealth" gives capital ownership to
    whoever already has money, so dividends amplify the starting distribution;
    "equal" gives everyone the same slice, so the same firms and the same
    profits flow back evenly. Running both isolates what concentrated capital
    OWNERSHIP does, holding the economy's production identical -- which is the
    question a redistribution-only matrix cannot ask, because it only ever
    moves income after the fact.

    Deterministic by construction: proportional allocation, no rng draws.
    """
    if config.share_allocation == "none":
        return {}
    if config.share_allocation not in ("wealth", "equal"):
        raise ValueError(f"unknown share_allocation {config.share_allocation!r}")

    if config.share_allocation == "wealth":
        weights = {eid: float(starting_balance.get(eid, 0)) for eid in individual_ids}
        if sum(weights.values()) <= 0:
            raise ValueError("wealth allocation needs positive starting balances")
    else:
        weights = {eid: 1.0 for eid in individual_ids}

    total_weight = sum(weights.values())
    held: dict[str, Decimal] = {eid: Decimal("0") for eid in individual_ids}

    for i in range(config.n_firms):
        symbol = share_symbol(i)
        # Largest-remainder so every firm's shares sum to exactly
        # shares_per_firm: rounding each slice independently would leak or
        # mint fractions of a company, and the dividend divides by the
        # register's live total.
        exact = {
            eid: config.shares_per_firm * Decimal(str(w / total_weight))
            for eid, w in weights.items()
        }
        floors = {eid: v.quantize(Decimal("1"), rounding=ROUND_DOWN) for eid, v in exact.items()}
        shortfall = int(config.shares_per_firm - sum(floors.values()))
        # Ties broken by entity id, so the leftovers are assigned the same way
        # on every run rather than by dict iteration accident.
        ranked = sorted(
            individual_ids, key=lambda e: (-(exact[e] - floors[e]), e)
        )
        for eid in ranked[:shortfall]:
            floors[eid] += 1

        for eid, qty in floors.items():
            if qty > 0:
                markets.adjust_holding(session, session.get(Entity, eid), symbol, qty)
                held[eid] += qty

    return held


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
                # The nominal anchor, exposed so it can be swept. These were
                # Lua constants tuned for an economy whose only recurring
                # purchase was food; with rent and power they decide whether
                # the basket is affordable at all. See calibrate.py.
                "planning_horizon": str(config.planning_horizon),
                "food_budget_share": str(config.food_budget_share),
                "shelter_budget_share": str(config.shelter_budget_share),
                "energy_budget_share": str(config.energy_budget_share),
                "clothes_budget_share": str(config.clothes_budget_share),
                "work_free_cover": str(config.work_free_cover),
                "min_wage": str(config.min_wage),
            },
        ))

    for firm_index, entity_id in enumerate(firm_ids):
        session.add(Script(
            name=f"firm-behaviour-{entity_id}",
            script_type=ScriptType.BEHAVIOUR,
            source=firm_source,
            entity_id=entity_id,
            timeout_ms=200,
            state={
                # Empty share_symbol disables the dividend block entirely, so
                # a no-shares run executes exactly the code it did before.
                "share_symbol": (
                    share_symbol(firm_index) if config.share_allocation != "none" else ""
                ),
                "firm_margin": str(config.firm_margin),
                "firm_cash_reserve": str(config.firm_cash_reserve),
                "dividend_period": config.dividend_period,
                "dividend_payout": str(config.dividend_payout),
                "dividend_timer": 0,
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
                # Seed for the per-firm RNG firm.lua reseeds every tick (a
                # fresh LuaRuntime is built per call, so Lua's own
                # math.random state never persists). Used to jitter build
                # timing so firms don't all break ground the same tick.
                "rng": rng.randrange(1, 2000000000),
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
