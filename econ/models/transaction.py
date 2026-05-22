import uuid
import enum
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import String, Numeric, DateTime, Enum as SAEnum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class TransactionType(enum.Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)  # always positive
    tx_type: Mapped[TransactionType] = mapped_column(SAEnum(TransactionType), nullable=False)
    from_account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=True)
    to_account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("accounts.id"), nullable=True)
    reference: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    account: Mapped["Account"] = relationship("Account", foreign_keys=[account_id], back_populates="transactions")
    from_account: Mapped["Account | None"] = relationship("Account", foreign_keys=[from_account_id])
    to_account: Mapped["Account | None"] = relationship("Account", foreign_keys=[to_account_id])

    def __repr__(self) -> str:
        return f"<Transaction id={self.id} {self.tx_type.value} {self.amount} {self.account.currency if self.account else ''}>"
