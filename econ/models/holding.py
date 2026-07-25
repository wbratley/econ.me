import uuid
from decimal import Decimal
from sqlalchemy import String, Numeric, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("entity_id", "symbol", name="uq_holdings_entity_symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    # invariant quantity >= 0 is enforced in the service layer (adjust_holding)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0")
    )

    entity: Mapped["Entity"] = relationship("Entity")

    def __repr__(self) -> str:
        return f"<Holding entity={self.entity_id} {self.quantity} {self.symbol}>"
