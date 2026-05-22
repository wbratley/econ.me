import uuid
import enum
from sqlalchemy import String, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class EntityType(enum.Enum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"
    BANK = "bank"
    GOVERNMENT = "government"


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(SAEnum(EntityType), nullable=False)

    accounts: Mapped[list["Account"]] = relationship("Account", back_populates="entity")

    def __repr__(self) -> str:
        return f"<Entity id={self.id} name={self.name!r} type={self.entity_type.value}>"
