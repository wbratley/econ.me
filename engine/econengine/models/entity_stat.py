import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class EntityStat(Base):
    """One combat stat for one entity: ATTACK, DEFENSE, ... — authored
    content, not code. Stats are the entity side of combat (combat.py):
    a creature is what it carries (holdings) plus what it is (rows
    here). The rules that turn stats into outcomes — weapon bonuses,
    deterrence, loot — live in the pack's COMBAT_RULES world setting;
    this table is just the numbers each creature was born with."""

    __tablename__ = "entity_stats"
    __table_args__ = (UniqueConstraint("entity_id", "stat"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    stat: Mapped[str] = mapped_column(String(32), nullable=False)  # uppercase, e.g. ATTACK
    value: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"<EntityStat {self.entity_id} {self.stat}={self.value}>"
