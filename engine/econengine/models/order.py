import uuid
import enum
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import String, Numeric, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class OrderSide(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(enum.Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    market_id: Mapped[str] = mapped_column(String(36), ForeignKey("markets.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    # settlement account: buyer pays from it, seller receives into it
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False)
    side: Mapped[OrderSide] = mapped_column(SAEnum(OrderSide), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    remaining: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus), nullable=False, default=OrderStatus.OPEN
    )
    reference: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    cancel_reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # price-time priority key
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    market: Mapped["Market"] = relationship("Market")
    entity: Mapped["Entity"] = relationship("Entity")
    account: Mapped["Account"] = relationship("Account")

    def __repr__(self) -> str:
        return f"<Order {self.side.value} {self.remaining}/{self.quantity} @ {self.limit_price} [{self.status.value}]>"
