import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Boolean, String, Numeric, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # uppercase, e.g. WHEAT
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(
        String(1000), nullable=False, default=""
    )  # authored catalog text (Phase 3a)
    pack_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # the pack that installed this row (§15.4); NULL = platform/legacy
    currency: Mapped[str] = mapped_column(String(8), nullable=False)  # quote currency, e.g. USD or COIN
    # Spatial seat (docs/spatial.md S2, Fork 5): NULL = the global market
    # of today, reachable from anywhere. A placed market trades only for
    # entities standing there — place_order refuses the elsewhere.
    place_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("places.id"), nullable=True
    )
    place: Mapped["Place | None"] = relationship("Place")
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(precision=18, scale=4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<Market symbol={self.symbol} currency={self.currency} last_price={self.last_price}>"
