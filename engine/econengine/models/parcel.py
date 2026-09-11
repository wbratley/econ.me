import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Integer, String, Numeric, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class Parcel(Base):
    """A non-fungible piece of land: an owner (entity, NULL while unclaimed),
    a region id, and a zoning/type tag. The engine deliberately knows nothing
    about meters or meshes — extent_ref is an opaque reference the world layer
    resolves to geometry; the engine only records who controls which parcel
    and what stands on it (design.md § parcels)."""

    __tablename__ = "parcels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    parcel_type: Mapped[str] = mapped_column(String(32), nullable=False)  # uppercase zoning tag, e.g. FIELD
    region_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    extent_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    owner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("entities.id"), nullable=True)
    # The parcel's resource node (docs/spatial.md S1): where this parcel IS
    # on the pack's map. Nullable — parcels predate places, and abstract
    # worlds never set one. Presence gates read it in S2; S1 only records
    # the join.
    place_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("places.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    owner: Mapped["Entity"] = relationship("Entity")
    place: Mapped["Place | None"] = relationship("Place", back_populates="parcels")
    facilities: Mapped[list["Facility"]] = relationship(
        "Facility", back_populates="parcel", cascade="all, delete-orphan",
        order_by="Facility.created_at",
    )
    deposits: Mapped[list["Deposit"]] = relationship(
        "Deposit", back_populates="parcel", cascade="all, delete-orphan",
        order_by="Deposit.symbol",
    )

    def __repr__(self) -> str:
        return f"<Parcel {self.parcel_type} owner={self.owner_id or 'UNCLAIMED'}>"


class Facility(Base):
    """A built improvement standing on a parcel (FARM, SMITHY, …). Facilities
    enter the world through construction recipes (builds_facility) or genesis
    placement; recipes that require one make production *located*.

    The commons columns (P1, the fire rework): `access` decides who may bind
    a process to it -- OWNER (default, the parcel controller only, all of
    history's behavior) or PLACE (public: any active entity; localize with
    the recipe's own presence gates, the way a village fire warms whoever
    walks up to it). `capacity` is how many processes may hold it at once
    (one smithy, one smelt; a four-stone hearth seats four). `fuel` /
    `fuel_capacity` / `fuel_burn_per_tick` give a facility a burning stock:
    stoking credits fuel (never past fuel_capacity), the tick pass burns it
    down, and fuel reaching zero is the fire going dark -- a facility that
    consumes to stay lit, the recurring-cost shape processes already had."""

    __tablename__ = "facilities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    parcel_id: Mapped[str] = mapped_column(String(36), ForeignKey("parcels.id"), nullable=False)
    facility_type: Mapped[str] = mapped_column(String(32), nullable=False)  # uppercase, e.g. SMITHY
    built_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL: genesis placement
    # who may bind: "OWNER" (the parcel controller) or "PLACE" (public)
    access: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OWNER", server_default="OWNER"
    )
    # concurrent binder slots (running processes of the required type)
    capacity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # the burning stock: stoked up (capped at fuel_capacity), burned down
    # fuel_burn_per_tick each tick; zero = dark. "Unusable when dark" is the
    # PACK's business -- the engine only counts and reports.
    fuel: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0"),
        server_default="0",
    )
    fuel_capacity: Mapped[Decimal | None] = mapped_column(Numeric(precision=18, scale=4), nullable=True)
    fuel_burn_per_tick: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0"),
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    parcel: Mapped["Parcel"] = relationship("Parcel", back_populates="facilities")

    def __repr__(self) -> str:
        return f"<Facility {self.facility_type} parcel={self.parcel_id}>"


class Deposit(Base):
    """A natural resource dotted onto a parcel: a remaining quantity and
    optionally regeneration (regen_per_tick toward capacity). Deposits deplete
    only through extraction recipes' deposit_inputs — geography *is* the
    resource distribution. Placement is genesis data."""

    __tablename__ = "deposits"
    __table_args__ = (UniqueConstraint("parcel_id", "symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    parcel_id: Mapped[str] = mapped_column(String(36), ForeignKey("parcels.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    # regeneration ceiling; NULL for non-regenerating deposits
    capacity: Mapped[Decimal | None] = mapped_column(Numeric(precision=18, scale=4), nullable=True)
    regen_per_tick: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    parcel: Mapped["Parcel"] = relationship("Parcel", back_populates="deposits")

    def __repr__(self) -> str:
        return f"<Deposit {self.symbol} {self.quantity} parcel={self.parcel_id}>"
