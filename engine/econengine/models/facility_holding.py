import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class FacilityHolding(Base):
    """Goods physically at a facility's shelf — per (facility, owner, symbol)
    (docs/camp-storage.md).

    owner = an entity id -> that entity's PRIVATE store (only it may touch
    the row); owner = "*" -> the SHARED pool at the facility (any entity may
    deposit/pickup). The drop/pickup pair is what makes theft and scavenging
    emergent: once dropped, goods are in a pool nobody owns. Pools rot like
    carried goods (apply_decay) and a house's private rows burn/inherit with
    the estate; shared rows outlive their dropper.

    Mirrors MarketEscrow (S5b) but keys on a facility and an owner; the
    facility's `capacity` is a private-store policy only (shared rows skip
    it), enforced in the service, as quantity >= 0 is.
    """

    __tablename__ = "facility_holdings"
    __table_args__ = (
        UniqueConstraint("facility_id", "owner_id", "symbol",
                         name="uq_facility_holding"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    facility_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("facilities.id"), nullable=False
    )
    # NOT NULL, no FK: an entity id (private row) or the "*" shared-pool
    # sentinel. SQLite treats NULLs as distinct in a UNIQUE index, which
    # would allow duplicate shared rows — the sentinel keeps the constraint
    # DB-enforced. The service checks ownership; the DB does not.
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<FacilityHolding facility={self.facility_id} owner={self.owner_id} "
            f"symbol={self.symbol} quantity={self.quantity}>"
        )
