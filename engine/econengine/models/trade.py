import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Integer, String, Numeric, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    market_id: Mapped[str] = mapped_column(String(36), ForeignKey("markets.id"), nullable=False)
    # int, not FK: the auction runs before the Tick row is flushed
    tick_number: Mapped[int] = mapped_column(Integer, nullable=False)
    buy_order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), nullable=False)
    sell_order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), nullable=False)
    buyer_entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    seller_entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    market: Mapped["Market"] = relationship("Market")

    def __repr__(self) -> str:
        return f"<Trade {self.quantity} @ {self.price} tick={self.tick_number}>"
