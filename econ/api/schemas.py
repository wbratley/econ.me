from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_serializer

from econ.models.entity import EntityType
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


class TickRead(BaseModel):
    id: str
    number: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    events: list[dict] = []

    model_config = {"from_attributes": True}
