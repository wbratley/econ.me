import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import Integer, String, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class ProcessStatus(enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Process(Base):
    """Work in progress: a recipe started by an entity. Inputs were consumed
    at start; the tick engine credits outputs once the current tick number
    reaches completes_tick."""

    __tablename__ = "processes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    # ints, not FKs: processes reference ticks that may not have flushed yet
    started_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    completes_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ProcessStatus] = mapped_column(
        SAEnum(ProcessStatus), nullable=False, default=ProcessStatus.RUNNING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    recipe: Mapped["Recipe"] = relationship("Recipe")
    entity: Mapped["Entity"] = relationship("Entity")

    def __repr__(self) -> str:
        return f"<Process recipe={self.recipe_id} entity={self.entity_id} [{self.status.value}] completes={self.completes_tick}>"
