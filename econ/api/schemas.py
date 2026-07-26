from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, field_serializer

from econ.models.entity import EntityType
from econ.models.order import OrderSide, OrderStatus
from econ.models.process import ProcessStatus
from econ.models.transaction import TransactionType
from econ.models.script import ScriptType


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


class ScriptRead(BaseModel):
    id: str
    name: str
    description: str
    script_type: ScriptType
    source: str
    is_active: bool
    timeout_ms: int
    entity_id: Optional[str] = None
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


class RecipeCreate(BaseModel):
    code: str
    name: str = ""
    duration_ticks: int
    inputs: dict[str, str] = {}   # symbol -> quantity
    outputs: dict[str, str]


class RecipeUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class RecipeRead(BaseModel):
    id: str
    code: str
    name: str
    duration_ticks: int
    is_active: bool
    inputs: list[RecipeItemRead] = []
    outputs: list[RecipeItemRead] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class ProcessCreate(BaseModel):
    entity_id: str
    recipe: str


class ProcessRead(BaseModel):
    id: str
    recipe_id: str
    entity_id: str
    started_tick: int
    completes_tick: int
    status: ProcessStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class GoodCreate(BaseModel):
    symbol: str
    name: str = ""
    decay_per_tick: str = "0"
    auto_issue_quantity: str = "0"
    auto_issue_entity_type: Optional[EntityType] = None


class GoodUpdate(BaseModel):
    name: Optional[str] = None
    decay_per_tick: Optional[str] = None
    auto_issue_quantity: Optional[str] = None
    auto_issue_entity_type: Optional[EntityType] = None  # explicit null clears it


class GoodRead(BaseModel):
    id: str
    symbol: str
    name: str
    decay_per_tick: Decimal
    auto_issue_quantity: Decimal
    auto_issue_entity_type: Optional[EntityType] = None
    created_at: datetime

    @field_serializer("decay_per_tick", "auto_issue_quantity")
    def _decimals(self, v: Decimal) -> str:
        return str(v)

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
