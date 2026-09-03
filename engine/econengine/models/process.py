import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import Integer, String, DateTime, ForeignKey, Boolean, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class ProcessStatus(enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    # a RUNNING process that could not meet a per-tick input demand (see
    # production.consume_per_tick_inputs): abandoned mid-run, outputs and
    # unlocks forfeited, like a cancellation but engine-initiated for want of
    # an input rather than owner-initiated
    FAILED = "failed"


class Process(Base):
    """Work in progress: a recipe started by an entity. Inputs were consumed
    at start; the tick engine credits outputs once the current tick number
    reaches completes_tick."""

    __tablename__ = "processes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    # parcel-bound production: set when the recipe requires a facility, draws
    # a deposit, or builds a facility; the parcel was controlled at start
    parcel_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("parcels.id"), nullable=True)
    # ints, not FKs: processes reference ticks that may not have flushed yet
    started_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    completes_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ProcessStatus] = mapped_column(
        SAEnum(ProcessStatus), nullable=False, default=ProcessStatus.RUNNING
    )
    # stochastic recipes only: the audit trail of the completion roll —
    # outcome_roll = H(prev tick's events_hash ":" process id), outcome_branch
    # the selected row's position. NULL until completed / for plain recipes.
    outcome_branch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_roll: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Travel (docs/spatial.md S3): a hop in a TravelRoute. Ordinary
    # start_process machinery (inputs, requirements, gates) created it,
    # but the road sets its duration (completes_tick = start + edge
    # cost_ticks) and its completion moves the entity (travel.complete_travel)
    # instead of crediting production statistics.
    is_travel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    edge_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("spatial_edges.id"), nullable=True)
    route_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("travel_routes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    recipe: Mapped["Recipe"] = relationship("Recipe")
    entity: Mapped["Entity"] = relationship("Entity")
    parcel: Mapped["Parcel"] = relationship("Parcel")

    def __repr__(self) -> str:
        return f"<Process recipe={self.recipe_id} entity={self.entity_id} [{self.status.value}] completes={self.completes_tick}>"
