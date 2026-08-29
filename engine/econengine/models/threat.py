import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import Boolean, String, Numeric, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base
from .entity import EntityType


class Threat(Base):
    """A declared per-tick danger for entities of a type: pressure that
    accrues during dark hours and credits a condition holding, scaled by
    how loud the entity was that tick. Like needs, threats are data —
    the world's predators are rows, not code paths.

    The deterrence rule is one holding read: an entity holding at least
    ``deterred_by_quantity`` of ``deterred_by_symbol`` has its whole
    pressure rate multiplied by ``deterrence_factor`` — the lit hearth
    keeps the pack shy, not deaf. Whether to stand by that hearth (and
    whether to answer it with a weapon once the pressure lands) stays
    the entity's decision: threats pressurize, they never act.
    """

    __tablename__ = "threats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # uppercase, e.g. WOLF
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(
        String(1000), nullable=False, default=""
    )  # authored catalog text
    pack_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # the pack that installed this row (§15.4); NULL = platform/legacy
    entity_type: Mapped[EntityType | None] = mapped_column(
        SAEnum(EntityType), nullable=True
    )  # NULL = every entity
    # The condition credited by pressure (docs/design.md § conditions);
    # decay_per_tick on that good is the pressure's natural fade.
    condition_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    # Pressure per dark hour with nothing heard from the entity.
    ambient_night_per_tick: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False
    )
    # Extra pressure per delivered say THIS tick — noise carries at night.
    per_say_night: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0")
    )
    deterred_by_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deterred_by_quantity: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False, default=Decimal("0")
    )
    deterrence_factor: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=4), nullable=False, default=Decimal("1")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (f"<Threat {self.code} condition={self.condition_symbol} "
                f"ambient_night={self.ambient_night_per_tick}>")
