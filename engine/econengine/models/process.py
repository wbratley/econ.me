import uuid
import enum
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Integer, String, DateTime, ForeignKey, Boolean, Numeric, Enum as SAEnum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class ProcessStatus(enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    # a RUNNING process that could not meet a per-tick input demand (see
    # production.consume_per_tick_inputs): abandoned mid-run, outputs and
    # unlocks not credited — but the ticks already paid are banked in the
    # resume ledger (ProcessProgress below), so restarting the same recipe
    # resumes where the work stopped instead of forfeiting it
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
    # scaled recipes only (recipes.scales_with, P5): the factor the herd
    # priced this harvest at — min(cap, units of the scaling symbol held
    # at completion). NULL = the recipe paid its declared outputs as-is.
    scale_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=4), nullable=True
    )
    # Effort (docs/productivity.md §1): the fraction of the recipe's
    # declared LABOR input actually paid at start (NULL = full effort,
    # reads as 1). A short ration is not a refusal — it is a weaker
    # start; the factor is applied to credited outputs at completion.
    effort_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=4), nullable=True
    )
    # Productivity (docs/productivity.md §2): the P actually applied at
    # completion — the product of the productivity_factor of every
    # condition good held (qty > 0) — stamped next to scale_factor /
    # effort_factor. NULL = no tax (reads 1): an immune recipe, or a
    # worker with no productivity-taxing condition. Audit story on one
    # row: "why 1.4 berries?" → herd × effort × P.
    productivity: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=4), nullable=True
    )
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


class ProcessProgress(Base):
    """Work already paid into a recipe by an entity — the resume ledger
    (the run-69 one-rung trap). When a RUNNING process dies for want of a
    per-tick input (or is cancelled), the ticks it already paid are banked
    here instead of vanishing; starting the same recipe again resumes
    where the work stopped (no second lump draw, `completes_tick` set to
    the remaining ticks). One row per (entity, recipe); cleared when a
    process of that recipe completes — the credit is time, and it is
    spent by finishing the job."""

    __tablename__ = "process_progress"
    __table_args__ = (
        UniqueConstraint("entity_id", "recipe_id", name="uq_process_progress"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False, index=True)
    recipe_id: Mapped[str] = mapped_column(String(36), ForeignKey("recipes.id"), nullable=False)
    ticks_paid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The weakest effort any banked run started at (NULL = full): a
    # restart re-prices the completion at the effort already paid, so a
    # short-ration start cannot be laundered into a full one by failing.
    effort: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=4), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    entity: Mapped["Entity"] = relationship("Entity")
    recipe: Mapped["Recipe"] = relationship("Recipe")

    def __repr__(self) -> str:
        return (f"<ProcessProgress entity={self.entity_id} "
                f"recipe={self.recipe_id} ticks_paid={self.ticks_paid}>")
