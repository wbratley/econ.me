import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Boolean, Integer, String, Numeric, DateTime, ForeignKey, UniqueConstraint, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base
from .entity import EntityType


class Need(Base):
    """A declared per-tick demand for entities of a type: which holding
    symbols satisfy it (1:1) and how much is required each tick. The demand
    side of the economy is data — it emerges from which needs exist, not
    from code."""

    __tablename__ = "needs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # uppercase, e.g. FOOD
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    entity_type: Mapped[EntityType | None] = mapped_column(
        SAEnum(EntityType), nullable=True
    )  # NULL = every entity
    quantity_per_tick: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # lower = more essential; consumed first when needs share a satisfying good
    # Unmet-tick consequence (docs/design.md § conditions): the consumption
    # pass credits condition_symbol scaled by the shortfall — the memory of
    # deprivation lives in the holding, NeedState stays instantaneous.
    condition_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    condition_quantity: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0")
    )  # granted per fully-unmet tick; scaled by (1 - satisfaction)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    satisfiers: Mapped[list["NeedSatisfier"]] = relationship(
        "NeedSatisfier", back_populates="need", cascade="all, delete-orphan", order_by="NeedSatisfier.symbol"
    )

    def __repr__(self) -> str:
        return f"<Need {self.code} quantity={self.quantity_per_tick} priority={self.priority}>"


class NeedSatisfier(Base):
    __tablename__ = "need_satisfiers"
    __table_args__ = (UniqueConstraint("need_id", "symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    need_id: Mapped[str] = mapped_column(String(36), ForeignKey("needs.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    need: Mapped["Need"] = relationship("Need", back_populates="satisfiers")


class NeedState(Base):
    """Per-(entity, need) satisfaction score, rewritten by each tick's
    consumption pass: consumed / required, 1.0000 iff fully met."""

    __tablename__ = "need_states"
    __table_args__ = (UniqueConstraint("entity_id", "need_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    need_id: Mapped[str] = mapped_column(String(36), ForeignKey("needs.id"), nullable=False)
    satisfaction: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=4), nullable=False)
    updated_tick: Mapped[int] = mapped_column(Integer, nullable=False)

    need: Mapped["Need"] = relationship("Need")

    def __repr__(self) -> str:
        return f"<NeedState entity={self.entity_id} need={self.need_id} satisfaction={self.satisfaction}>"
