from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Union

from pydantic import AliasChoices, BaseModel, Field, field_serializer, field_validator

from econengine.models.entity import EntityStatus, EntityType
from econengine.models.order import OrderSide, OrderStatus
from econengine.models.process import ProcessStatus
from econengine.models.transaction import TransactionType
from econengine.models.script import ScriptType
from econengine.models.technology import TechScope


class EntityCreate(BaseModel):
    name: str
    entity_type: EntityType


class AdminEntityCreate(BaseModel):
    name: str
    entity_type: EntityType
    owner_id: Optional[str] = None


class EntityUpdate(BaseModel):
    name: Optional[str] = None
    entity_type: Optional[EntityType] = None
    is_monetary_authority: Optional[bool] = None
    capabilities: Optional[list[str]] = None
    heir_id: Optional[str] = None  # explicit null clears it


class AccountRead(BaseModel):
    id: str
    entity_id: str
    currency: str
    balance: Decimal

    @field_serializer("balance")
    def _balance(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class EntityRead(BaseModel):
    id: str
    name: str
    entity_type: EntityType
    owner_id: Optional[str] = None
    is_monetary_authority: bool = False
    capabilities: list[str] = []
    status: EntityStatus = EntityStatus.ACTIVE
    incapacitated_tick: Optional[int] = None
    heir_id: Optional[str] = None
    accounts: list[AccountRead] = []

    model_config = {"from_attributes": True}


class AccountCreate(BaseModel):
    currency: str
    initial_balance: Decimal = Decimal("0")


class TransactionRead(BaseModel):
    id: str
    account_id: str
    date: datetime
    amount: Decimal
    tx_type: TransactionType
    from_account_id: Optional[str] = None
    to_account_id: Optional[str] = None
    reference: str

    @field_serializer("amount")
    def _amount(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class DepositRequest(BaseModel):
    account_id: str
    amount: str
    reference: str


class WithdrawRequest(BaseModel):
    account_id: str
    amount: str
    reference: str


class TransferRequest(BaseModel):
    from_account_id: str
    to_account_id: str
    amount: str
    reference: str


class IssueRequest(BaseModel):
    account_id: str
    amount: str
    reference: str


class RetireRequest(BaseModel):
    account_id: str
    amount: str
    reference: str


class UserRead(BaseModel):
    id: str
    email: str
    name: str
    is_admin: bool

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    name: Optional[str] = None
    is_admin: Optional[bool] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ScriptCreate(BaseModel):
    name: str
    description: str = ""
    script_type: ScriptType
    source: str
    timeout_ms: int = 100
    entity_id: Optional[str] = None
    lineage_id: Optional[str] = None


class BehaviourScriptWrite(BaseModel):
    """Body for the ownership-gated autonomy path
    (``POST /entities/{id}/behaviour``; docs/game.md §6). A player rewrites
    the BEHAVIOUR script of an entity they own. ``script_type`` is fixed to
    BEHAVIOUR by the endpoint -- autonomy may not touch POLICY / VALIDATOR /
    HOOK."""
    source: str
    description: str = ""
    timeout_ms: int = 100


class ScriptRead(BaseModel):
    id: str
    name: str
    description: str
    script_type: ScriptType
    source: str
    is_active: bool
    timeout_ms: int
    entity_id: Optional[str] = None
    lineage_id: Optional[str] = None
    state: dict = {}
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScriptUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    is_active: Optional[bool] = None
    timeout_ms: Optional[int] = None
    entity_id: Optional[str] = None


class ProposalRead(BaseModel):
    """A proposal in the democracy layer (actors step 4a-ii) — a batch of
    proposed mutations plus the weight model, threshold, and quorum that
    define the form of government deciding it."""
    id: str
    title: str
    proposer_id: str
    target_id: str
    proposal_type: str = "ordinary"
    weight_model: str
    threshold: str
    quorum: str
    mutations: list
    status: str
    created_at: datetime
    enacted_at: Optional[datetime] = None
    tally_yes: Optional[str] = None
    tally_no: Optional[str] = None
    tally_electorate: Optional[str] = None
    tally_turnout: Optional[str] = None
    failure_reason: Optional[str] = None

    model_config = {"from_attributes": True}


class VoteRead(BaseModel):
    """One entity's for/against on one proposal, weight snapshotted at cast."""
    id: str
    proposal_id: str
    voter_id: str
    choice: str
    weight: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ScriptValidateResult(BaseModel):
    ok: bool
    error: Optional[str] = None
    intents: list[dict] = []
    return_value: Any = None


class TickRead(BaseModel):
    id: str
    number: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    events: list[dict] = []
    events_hash: Optional[str] = None  # sha256 commitment over `events`

    model_config = {"from_attributes": True}


class MarketCreate(BaseModel):
    symbol: str
    currency: str
    name: str = ""


class MarketUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class MarketRead(BaseModel):
    id: str
    symbol: str
    name: str
    currency: str
    last_price: Optional[Decimal] = None
    is_active: bool
    created_at: datetime

    @field_serializer("last_price")
    def _last_price(self, v: Optional[Decimal]) -> Optional[str]:
        return None if v is None else str(v)

    model_config = {"from_attributes": True}


class HoldingRead(BaseModel):
    id: str
    entity_id: str
    symbol: str
    quantity: Decimal

    @field_serializer("quantity")
    def _quantity(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class HoldingGrant(BaseModel):
    entity_id: str
    symbol: str
    delta: str  # signed; the goods faucet


class OrderCreate(BaseModel):
    symbol: str
    side: OrderSide
    quantity: str
    limit_price: str
    account_id: str
    reference: str = ""


class OrderRead(BaseModel):
    id: str
    market_id: str
    entity_id: str
    account_id: str
    side: OrderSide
    quantity: Decimal
    remaining: Decimal
    limit_price: Decimal
    status: OrderStatus
    reference: str
    cancel_reason: str
    created_at: datetime

    @field_serializer("quantity", "remaining", "limit_price")
    def _decimals(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class RecipeItemRead(BaseModel):
    symbol: str
    quantity: Decimal

    @field_serializer("quantity")
    def _quantity(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class RecipeBranchCreate(BaseModel):
    weight: str                   # relative odds; need not sum to 1
    outputs: dict[str, str] = {}  # may be empty: a total-loss branch
    label: str = ""


class RecipeBranchRead(BaseModel):
    position: int
    weight: Decimal
    label: str
    outputs: list[RecipeItemRead] = []

    @field_serializer("weight")
    def _weight(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class RecipeCreate(BaseModel):
    code: str
    name: str = ""
    duration_ticks: int
    inputs: dict[str, str] = {}   # symbol -> quantity
    outputs: dict[str, str] = {}  # may be empty for pure research recipes
    branches: list[RecipeBranchCreate] = []  # outcome table; excludes outputs
    requires: list[str] = []      # technology codes gating the recipe
    unlocks: list[str] = []       # technology codes granted on completion
    good_requirements: dict[str, str] = {}  # held-but-not-consumed (machinery)
    deposit_inputs: dict[str, str] = {}     # drawn from the bound parcel's deposits
    requires_facility: Optional[str] = None  # facility type on the bound parcel
    builds_facility: Optional[str] = None    # construction: erected at completion


class RecipeUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class RecipeRead(BaseModel):
    id: str
    code: str
    name: str
    duration_ticks: int
    is_active: bool
    requires_facility: Optional[str] = None
    builds_facility: Optional[str] = None
    inputs: list[RecipeItemRead] = []
    outputs: list[RecipeItemRead] = []
    branches: list[RecipeBranchRead] = []
    good_requirements: list[RecipeItemRead] = []
    deposit_inputs: list[RecipeItemRead] = []
    # the ORM relationship is named `requirements`; the API field is `requires`
    requires: list[str] = Field(default=[], validation_alias=AliasChoices("requires", "requirements"))
    unlocks: list[str] = []
    created_at: datetime

    @field_validator("requires", "unlocks", mode="before")
    @classmethod
    def _technology_codes(cls, v):
        return sorted(t if isinstance(t, str) else t.technology.code for t in v)

    model_config = {"from_attributes": True}


class ProcessCreate(BaseModel):
    entity_id: str
    recipe: str
    parcel_id: Optional[str] = None  # required for parcel-bound recipes


class ProcessRead(BaseModel):
    id: str
    recipe_id: str
    entity_id: str
    parcel_id: Optional[str] = None
    started_tick: int
    completes_tick: int
    status: ProcessStatus
    outcome_branch: Optional[int] = None  # stochastic recipes, once completed
    outcome_roll: Optional[str] = None    # the audited hash, ditto
    created_at: datetime

    model_config = {"from_attributes": True}


class GoodCreate(BaseModel):
    symbol: str
    name: str = ""
    decay_per_tick: str = "0"
    auto_issue_quantity: str = "0"
    auto_issue_entity_type: Optional[EntityType] = None
    modifies_pattern: Optional[str] = None  # condition: glob over symbols
    modifies_factor: Optional[str] = None  # condition: effective-quantity multiplier
    incapacitates_at: Optional[str] = None  # condition: deactivation threshold


class GoodUpdate(BaseModel):
    name: Optional[str] = None
    decay_per_tick: Optional[str] = None
    auto_issue_quantity: Optional[str] = None
    auto_issue_entity_type: Optional[EntityType] = None  # explicit null clears it
    modifies_pattern: Optional[str] = None  # set with modifies_factor; explicit null clears both
    modifies_factor: Optional[str] = None
    incapacitates_at: Optional[str] = None  # explicit null clears it


class GoodRead(BaseModel):
    id: str
    symbol: str
    name: str
    decay_per_tick: Decimal
    auto_issue_quantity: Decimal
    auto_issue_entity_type: Optional[EntityType] = None
    modifies_pattern: Optional[str] = None
    modifies_factor: Optional[Decimal] = None
    incapacitates_at: Optional[Decimal] = None
    created_at: datetime

    @field_serializer("decay_per_tick", "auto_issue_quantity")
    def _decimals(self, v: Decimal) -> str:
        return str(v)

    @field_serializer("modifies_factor", "incapacitates_at")
    def _optional_decimals(self, v: Optional[Decimal]) -> Optional[str]:
        return None if v is None else str(v)

    model_config = {"from_attributes": True}


class NeedCreate(BaseModel):
    code: str
    name: str = ""
    entity_type: Optional[EntityType] = None  # null = every entity
    quantity_per_tick: str
    priority: int = 0
    satisfiers: list[str]
    condition_symbol: Optional[str] = None  # credited on unmet ticks
    condition_quantity: str = "0"  # per fully-unmet tick, scaled by shortfall


class NeedUpdate(BaseModel):
    name: Optional[str] = None
    quantity_per_tick: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    satisfiers: Optional[list[str]] = None  # replaces the whole list
    condition_symbol: Optional[str] = None  # set with condition_quantity; explicit null clears both
    condition_quantity: Optional[str] = None


class NeedRead(BaseModel):
    id: str
    code: str
    name: str
    entity_type: Optional[EntityType] = None
    quantity_per_tick: Decimal
    priority: int
    is_active: bool
    satisfiers: list[str]
    condition_symbol: Optional[str] = None
    condition_quantity: Decimal = Decimal("0")
    created_at: datetime

    @field_validator("satisfiers", mode="before")
    @classmethod
    def _satisfier_symbols(cls, v):
        return [s if isinstance(s, str) else s.symbol for s in v]

    @field_serializer("quantity_per_tick", "condition_quantity")
    def _quantity(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class EstateRuleUpdate(BaseModel):
    policy: str  # burn | heir | treasury
    treasury_entity_id: Optional[str] = None


class EstateRuleRead(BaseModel):
    policy: str
    treasury_entity_id: Optional[str] = None


class ComputeBudgetUpdate(BaseModel):
    budget_ms: Optional[int] = None  # null clears the budget (unlimited)


class ComputeBudgetRead(BaseModel):
    budget_ms: Optional[int] = None


#: ---------------------------------------------------------------------------
#: Player onboarding (docs/game.md §6, §12.6; Phase 1)
#: ---------------------------------------------------------------------------

class JoinConfigWrite(BaseModel):
    """Operator-set founder package: what a new player starts with. All
    fields optional; absent fields are left unchanged (merge semantics)."""
    endowment: Optional[Decimal] = None
    currency: Optional[str] = None
    starter_behaviour: Optional[str] = None


class JoinConfigRead(BaseModel):
    endowment: Decimal
    currency: str
    starter_behaviour: Optional[str] = None

    @field_serializer("endowment")
    def _endowment(self, v: Decimal) -> str:
        return str(v)


class JoinResult(BaseModel):
    """Returned by ``POST /join`` -- the founder entity, its endowment
    account, and the starter behaviour applied (None if the world has no
    starter configured)."""
    entity: EntityRead
    account: AccountRead
    behaviour: Optional[ScriptRead] = None


#: ---------------------------------------------------------------------------
#: Round scheduler -- the platform's batched-tick clock (game.md §9)
#: ---------------------------------------------------------------------------

class RoundState(BaseModel):
    """The round clock's current state (a read). ``current_round`` is the
    round open for submission; ``round_number`` is how many have resolved."""
    round_number: int
    current_round: int
    status: str
    ticks_run: int
    ticks_per_round: int
    ticks_into_round: int


class RoundSummary(BaseModel):
    """Returned by ``POST /admin/rounds/advance`` -- the round just resolved."""
    round_number: int          # the round that just completed
    ticks: list[int]           # tick numbers run this round
    events: int                # total events this round
    events_by_type: dict[str, int]
    next_round: int            # the now-open round
    ticks_per_round: int
    victory_stamps: list[dict] = []    # observer output, if an epoch ran (§14.2)
    eliminations: list[dict] = []      # dynasty extinctions stamped this round


#: ---------------------------------------------------------------------------
#: Epochs + victory observer (game.md §7, §14; Phase 2a)
#: ---------------------------------------------------------------------------

class EpochStart(BaseModel):
    """Start the next epoch: a ``{code, params}`` achievement spec from the
    §7 victory menu (accumulate / innovate / endure / grow)."""
    code: str
    params: dict = {}


class EpochRead(BaseModel):
    """The current/last epoch's state. ``running`` is derived (state exists
    and ``ended_tick`` is None); an absent epoch reads as running=False,
    number=0 -- the world simply plays without a victory condition."""
    running: bool
    number: int
    condition: Optional[dict] = None
    started_tick: int = 0
    ended_tick: Optional[int] = None
    winner_user_ids: list[str] = []


class EpochStatusRead(EpochRead):
    """Player view of the epoch: the world fact plus the caller's own
    elimination status (§14.3) -- the only dynasty-specific bit, and it is
    the caller's own."""
    eliminated_this_epoch: bool = False


class CouncilWrite(BaseModel):
    """Seed a council register. ``members`` may be a list of entity ids
    (an equal-weight council — every member weight 1) or a mapping
    ``{entity_id: weight}`` (a weighted council). The stored form is always
    ``{member_id: weight_str}``; the ``council`` model ignores weights and
    the ``weighted`` model honours them."""
    members: Union[list[str], dict[str, str]]


class CouncilRead(BaseModel):
    name: str
    members: dict[str, str]  # {member_entity_id: weight_str}


class DelegationWrite(BaseModel):
    """Seed a liquid-democracy delegation graph: a ``{delegator_id:
    delegate_id}`` mapping. A delegator's vote weight is redirected to their
    delegate (transitively); a self-loop is rejected at write time."""
    delegations: dict[str, str]


class DelegationRead(BaseModel):
    name: str
    delegations: dict[str, str]  # {delegator_id: delegate_id}


class IntentRequest(BaseModel):
    """A machine client's request for the shared intent resolver (§4.5
    'an intent API for machine clients' — same resolver as scripts use)."""
    entity_id: str
    type: str  # one of resolve_intent's dispatched intent_type values
    params: dict[str, str]
    priority: int = 100


class IntentResult(BaseModel):
    type: str
    entity_id: str
    params: dict
    idempotency_key: str
    status: str  # "applied" | "rejected"
    reason: Optional[str] = None
    order_id: Optional[str] = None    # present for place_order
    process_id: Optional[str] = None  # present for start_process
    script_id: Optional[str] = None   # present for set_script
    lineage_id: Optional[str] = None  # present for set_script
    proposal_id: Optional[str] = None     # present for create_proposal / vote / enact
    vote_id: Optional[str] = None         # present for vote
    proposal_status: Optional[str] = None  # present for enact ("enacted" | "failed")
    seized_goods: Optional[str] = None      # present for seize (quantity of goods)
    seized_symbol: Optional[str] = None     # present for seize (the goods symbol)
    seized_parcels: Optional[int] = None    # present for seize (parcel count)


class NeedStateRead(BaseModel):
    need: str
    satisfaction: Decimal
    updated_tick: int

    @field_serializer("satisfaction")
    def _satisfaction(self, v: Decimal) -> str:
        return str(v)


class TechnologyCreate(BaseModel):
    code: str
    name: str = ""
    scope: TechScope = TechScope.ENTITY
    prerequisites: list[str] = []  # codes of existing technologies


class TechnologyUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class TechnologyRead(BaseModel):
    id: str
    code: str
    name: str
    scope: TechScope
    is_active: bool
    prerequisites: list[str] = []
    created_at: datetime

    @field_validator("prerequisites", mode="before")
    @classmethod
    def _prerequisite_codes(cls, v):
        return sorted(p if isinstance(p, str) else p.prerequisite.code for p in v)

    model_config = {"from_attributes": True}


class UnlockGrant(BaseModel):
    entity_id: str


class UnlockRead(BaseModel):
    technology: str  # code
    entity_id: Optional[str] = None  # null = the whole world holds it
    unlocked_tick: int

    @field_validator("technology", mode="before")
    @classmethod
    def _technology_code(cls, v):
        return v if isinstance(v, str) else v.code

    model_config = {"from_attributes": True}


class TradeRead(BaseModel):
    id: str
    market_id: str
    tick_number: int
    buy_order_id: str
    sell_order_id: str
    buyer_entity_id: str
    seller_entity_id: str
    price: Decimal
    quantity: Decimal
    executed_at: datetime

    @field_serializer("price", "quantity")
    def _decimals(self, v: Decimal) -> str:
        return str(v)

    model_config = {"from_attributes": True}


class FacilityRead(BaseModel):
    id: str
    facility_type: str
    built_tick: Optional[int] = None  # null = genesis placement

    model_config = {"from_attributes": True}


class DepositRead(BaseModel):
    symbol: str
    quantity: Decimal
    capacity: Optional[Decimal] = None
    regen_per_tick: Decimal

    @field_serializer("quantity", "capacity", "regen_per_tick")
    def _decimals(self, v: Optional[Decimal]) -> Optional[str]:
        return None if v is None else str(v)

    model_config = {"from_attributes": True}


class ParcelCreate(BaseModel):
    parcel_type: str  # zoning tag, e.g. FIELD, LOT
    name: str = ""
    region_id: str = ""
    extent_ref: str = ""  # opaque world-layer geometry reference
    owner_entity_id: Optional[str] = None  # null = unclaimed


class ParcelRead(BaseModel):
    id: str
    name: str
    parcel_type: str
    region_id: str
    extent_ref: str
    owner_id: Optional[str] = None
    facilities: list[FacilityRead] = []
    deposits: list[DepositRead] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class ParcelTransfer(BaseModel):
    to_entity_id: str


class ParcelGrant(BaseModel):
    to_entity_id: Optional[str] = None  # null revokes to unclaimed


class FacilityCreate(BaseModel):
    facility_type: str  # genesis placement, admin only


class DepositCreate(BaseModel):
    symbol: str
    quantity: str
    capacity: Optional[str] = None  # required if regen_per_tick > 0
    regen_per_tick: str = "0"
