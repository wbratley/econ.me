import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class MarketEscrow(Base):
    """Goods physically AT a placed market — delivered by a seller, drawn
    by fills (docs/spatial.md S5b, "sellers must deliver").

    A pool per (market, seller, symbol), not a per-order lock: placement
    debits the seller's holdings into the pool, fills draw the pool down
    in lockstep with order.remaining, and cancel/expiry deliberately
    LEAVES the goods on the shelf — reclaiming is walking back to the
    market's place, not an intent. Global markets (place_id NULL) never
    create rows here; they keep the settle-from-holdings legacy path.
    """

    __tablename__ = "market_escrows"
    __table_args__ = (
        UniqueConstraint("market_id", "seller_entity_id", "symbol",
                         name="uq_market_escrow_pool"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    market_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("markets.id"), nullable=False
    )
    seller_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id"), nullable=False
    )
    # Redundant with market.symbol by construction (one market per
    # symbol), denormalised so the pool resolves without the join.
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
            f"<MarketEscrow market={self.market_id} seller={self.seller_entity_id} "
            f"symbol={self.symbol} quantity={self.quantity}>"
        )
