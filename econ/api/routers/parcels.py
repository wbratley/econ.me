from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine import parcels
from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.schemas import (
    DepositCreate, FacilityCreate, ParcelCreate, ParcelGrant, ParcelRead,
    ParcelTransfer,
)
from econengine.models import Entity, Parcel, User

router = APIRouter(tags=["parcels"])


def _parcel_or_404(session: Session, parcel_id: str) -> Parcel:
    parcel = parcels.get_parcel(session, parcel_id)
    if parcel is None:
        raise HTTPException(status_code=404, detail="Parcel not found")
    return parcel


def _decimal_or_422(raw: str, field: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail=f"Invalid {field}")


# ---------------------------------------------------------------------------
# The land registry is public: who controls which parcel and what stands on
# it is engine-recorded, readable by anyone (like recipes and markets).
# ---------------------------------------------------------------------------

@router.get("/parcels", response_model=list[ParcelRead])
def list_parcels(
    region_id: Optional[str] = None,
    owner_entity_id: Optional[str] = None,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    query = select(Parcel).order_by(Parcel.created_at, Parcel.id)
    if region_id is not None:
        query = query.where(Parcel.region_id == region_id)
    if owner_entity_id is not None:
        query = query.where(Parcel.owner_id == owner_entity_id)
    return session.execute(query).scalars().all()


@router.get("/parcels/{parcel_id}", response_model=ParcelRead)
def get_parcel(
    parcel_id: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _parcel_or_404(session, parcel_id)


@router.post("/parcels/{parcel_id}/transfer", response_model=ParcelRead)
def transfer_parcel(
    parcel_id: str,
    body: ParcelTransfer,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Ownership by the owner's intent: the parcel's owning entity must
    belong to the current user."""
    parcel = _parcel_or_404(session, parcel_id)
    owner = session.get(Entity, parcel.owner_id) if parcel.owner_id else None
    if owner is None or owner.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Parcel not found")
    to_entity = session.get(Entity, body.to_entity_id)
    if to_entity is None:
        raise HTTPException(status_code=422, detail="Unknown recipient entity")
    try:
        parcels.transfer_parcel(session, parcel_id, owner.id, to_entity)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(parcel)
    return parcel


# ---------------------------------------------------------------------------
# Admin: genesis data (parcels, facilities, deposits) and policy grants
# ---------------------------------------------------------------------------

@router.post("/admin/parcels", response_model=ParcelRead, status_code=201, tags=["admin"])
def create_parcel(
    body: ParcelCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    owner = None
    if body.owner_entity_id is not None:
        owner = session.get(Entity, body.owner_entity_id)
        if owner is None:
            raise HTTPException(status_code=422, detail="Unknown owner entity")
    parcel = parcels.create_parcel(
        session,
        body.parcel_type,
        name=body.name,
        region_id=body.region_id,
        extent_ref=body.extent_ref,
        owner=owner,
    )
    session.commit()
    session.refresh(parcel)
    return parcel


@router.post("/admin/parcels/{parcel_id}/grant", response_model=ParcelRead, tags=["admin"])
def grant_parcel(
    parcel_id: str,
    body: ParcelGrant,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Ownership by declared policy — the admin stand-in for land votes;
    to_entity_id null revokes the parcel to unclaimed."""
    parcel = _parcel_or_404(session, parcel_id)
    to_entity = None
    if body.to_entity_id is not None:
        to_entity = session.get(Entity, body.to_entity_id)
        if to_entity is None:
            raise HTTPException(status_code=422, detail="Unknown recipient entity")
    try:
        parcels.grant_parcel(session, parcel, to_entity)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(parcel)
    return parcel


@router.post("/admin/parcels/{parcel_id}/facilities", response_model=ParcelRead,
             status_code=201, tags=["admin"])
def add_facility(
    parcel_id: str,
    body: FacilityCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Genesis placement (built_tick null); built facilities come from
    construction recipes."""
    parcel = _parcel_or_404(session, parcel_id)
    parcels.add_facility(session, parcel, body.facility_type)
    session.commit()
    session.refresh(parcel)
    return parcel


@router.post("/admin/parcels/{parcel_id}/deposits", response_model=ParcelRead,
             status_code=201, tags=["admin"])
def add_deposit(
    parcel_id: str,
    body: DepositCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    parcel = _parcel_or_404(session, parcel_id)
    if parcels.get_deposit(session, parcel.id, body.symbol) is not None:
        raise HTTPException(status_code=409, detail="Deposit already exists")
    capacity = (
        _decimal_or_422(body.capacity, "capacity") if body.capacity is not None else None
    )
    try:
        parcels.add_deposit(
            session, parcel, body.symbol,
            quantity=_decimal_or_422(body.quantity, "quantity"),
            capacity=capacity,
            regen_per_tick=_decimal_or_422(body.regen_per_tick, "regen_per_tick"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(parcel)
    return parcel
