import uuid
import enum
from sqlalchemy import Boolean, Integer, String, Enum as SAEnum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class EntityType(enum.Enum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"
    BANK = "bank"
    GOVERNMENT = "government"


class EntityStatus(enum.Enum):
    """Engine lifecycle state. INCAPACITATED entities take no part in any
    tick pass and cannot act; permanent death is world policy layered on
    top (docs/design.md § conditions)."""

    ACTIVE = "active"
    INCAPACITATED = "incapacitated"


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(SAEnum(EntityType), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    is_monetary_authority: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[EntityStatus] = mapped_column(
        SAEnum(EntityStatus), nullable=False, default=EntityStatus.ACTIVE
    )
    incapacitated_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heir_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id"), nullable=True
    )  # estate recipient under the "heir" rule; unset falls back to burn

    accounts: Mapped[list["Account"]] = relationship("Account", back_populates="entity")
    owner: Mapped["User | None"] = relationship("User", back_populates="entities")

    def __repr__(self) -> str:
        return f"<Entity id={self.id} name={self.name!r} type={self.entity_type.value}>"
