import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import String, Numeric, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base
from .entity import EntityType


class Good(Base):
    """Optional per-symbol properties. Bare symbols work everywhere without a
    Good row; a row only adds behaviour (decay, auto-issue)."""

    __tablename__ = "goods"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    symbol: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)  # uppercase, e.g. LABOR
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    decay_per_tick: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=4), nullable=False, default=Decimal("0")
    )  # fraction of every holding lost at end of tick, 0..1
    auto_issue_quantity: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0")
    )  # top-up target per tick; 0 disables
    auto_issue_entity_type: Mapped[EntityType | None] = mapped_column(
        SAEnum(EntityType), nullable=True
    )  # NULL = every entity
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<Good symbol={self.symbol} decay={self.decay_per_tick}"
            f" auto_issue={self.auto_issue_quantity}>"
        )
