import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import Boolean, Integer, String, DateTime, ForeignKey, UniqueConstraint, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class TechScope(enum.Enum):
    ENTITY = "entity"  # each entity researches it for itself
    WORLD = "world"    # first discovery unlocks it for everyone


class Technology(Base):
    """A node in the tech DAG. Scope is a per-Technology column, not a world
    default: a smithing rank is per-person even in worlds where physics
    knowledge is shared (design.md §7). The DAG is acyclic by construction:
    prerequisites are fixed at creation and must already exist as rows, so no
    technology can ever reference itself or a later one."""

    __tablename__ = "technologies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # uppercase, e.g. SMELTING
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(
        String(1000), nullable=False, default=""
    )  # authored catalog text (Phase 3a)
    pack_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # the pack that installed this row (§15.4); NULL = platform/legacy
    scope: Mapped[TechScope] = mapped_column(SAEnum(TechScope), nullable=False, default=TechScope.ENTITY)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    prerequisites: Mapped[list["TechnologyPrerequisite"]] = relationship(
        "TechnologyPrerequisite",
        back_populates="technology",
        cascade="all, delete-orphan",
        foreign_keys="TechnologyPrerequisite.technology_id",
    )

    def __repr__(self) -> str:
        return f"<Technology {self.code} scope={self.scope.value}>"


class TechnologyPrerequisite(Base):
    __tablename__ = "technology_prerequisites"
    __table_args__ = (UniqueConstraint("technology_id", "prerequisite_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    technology_id: Mapped[str] = mapped_column(String(36), ForeignKey("technologies.id"), nullable=False)
    prerequisite_id: Mapped[str] = mapped_column(String(36), ForeignKey("technologies.id"), nullable=False)

    technology: Mapped["Technology"] = relationship(
        "Technology", back_populates="prerequisites", foreign_keys=[technology_id]
    )
    prerequisite: Mapped["Technology"] = relationship("Technology", foreign_keys=[prerequisite_id])


class Unlock(Base):
    """Possession of a technology. entity_id NULL means the whole world holds
    it (the technology's scope was WORLD when granted). Unlocks are never
    revoked — a lapsed master is credentialed but rusty (design.md)."""

    __tablename__ = "unlocks"
    __table_args__ = (UniqueConstraint("technology_id", "entity_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    technology_id: Mapped[str] = mapped_column(String(36), ForeignKey("technologies.id"), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("entities.id"), nullable=True)
    unlocked_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    technology: Mapped["Technology"] = relationship("Technology")

    def __repr__(self) -> str:
        holder = self.entity_id or "WORLD"
        return f"<Unlock {self.technology_id} holder={holder} tick={self.unlocked_tick}>"
