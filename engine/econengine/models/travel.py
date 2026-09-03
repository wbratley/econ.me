import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, JSON, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class TravelRouteStatus(enum.Enum):
    ACTIVE = "active"      # a hop is in flight (or about to be chained)
    ARRIVED = "arrived"    # the final hop completed; the entity stands at destination
    STRANDED = "stranded"  # stopped short: cancelled, failed, or the next hop refused


class TravelRoute(Base):
    """A journey in progress (docs/spatial.md S3): the auditable itinerary
    behind travel-as-Process. One row per ``travel`` intent; the hops are
    ordinary Processes (marked is_travel, bound to their edge) chained one
    at a time — the entity's location moves only on arrival, through
    places.move_entity, the single writer.

    ``hops`` is the PLANNED itinerary (a JSON list of edge ids, frozen at
    departure): the route is auditable and stable even if the map were
    re-authored underneath — what was walked is what was declared, and
    re-planning mid-journey is a new route, not a silent edit. ``modes``
    is the allow-list the traveller declared ([] = every mode).
    ``next_index`` points at the hop not yet started.

    Stranding is a real state, the pack's business (roadmap): cancellation,
    a failed per-tick input mid-road, or a next hop whose requirements the
    traveller can no longer meet all leave the entity standing at its last
    arrival — readable in the travel_stranded event, never a silent stop."""

    __tablename__ = "travel_routes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_id: Mapped[str] = mapped_column(String(36), ForeignKey("entities.id"), nullable=False)
    destination_place_id: Mapped[str] = mapped_column(String(36), ForeignKey("places.id"), nullable=False)
    # the declared mode allow-list, uppercased, sorted; [] = all modes
    modes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # the planned itinerary: edge ids in walk order, frozen at departure
    hops: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    next_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[TravelRouteStatus] = mapped_column(
        SAEnum(TravelRouteStatus), nullable=False, default=TravelRouteStatus.ACTIVE
    )
    # the in-flight hop's Process (None once arrived/stranded)
    current_process_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("processes.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    entity: Mapped["Entity"] = relationship("Entity")
    destination: Mapped["Place"] = relationship("Place")

    def __repr__(self) -> str:
        return f"<TravelRoute entity={self.entity_id[:8]} to={self.destination_place_id[:8]} [{self.status.value}] {self.next_index}/{len(self.hops)}>"
