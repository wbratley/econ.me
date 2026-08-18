import uuid
from decimal import Decimal
from sqlalchemy import String, Numeric, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)  # ISO 4217 or pack currency (COIN)
    balance: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False, default=Decimal("0"))

    entity: Mapped["Entity"] = relationship("Entity", back_populates="accounts")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction",
        foreign_keys="Transaction.account_id",
        back_populates="account",
    )

    def __repr__(self) -> str:
        return f"<Account id={self.id} currency={self.currency} balance={self.balance}>"
