import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class Place(Base):
    """A named spot on the world's map (docs/spatial.md S1): the engine's
    only spatial vocabulary beside parcels — an opaque ref the world layer
    may resolve to geometry (extent_ref), never a coordinate. Places are
    content rows like threats: the pack installs the map at genesis, keyed
    in its own namespace (pack_id provenance), and the engine records who
    stands where (`entities.location_place_id`) without ever knowing what
    a meter is. NULL location everywhere = the legacy abstract world,
    unchanged (Fork 6: unplaced is a citizen)."""

    __tablename__ = "places"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # uppercase, e.g. RIVER
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(
        String(1000), nullable=False, default=""
    )  # authored catalog text
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # uppercase tag: HEARTH, FOREST, POST...
    region_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # Opaque geometry hint for the world layer, ignored by the engine
    # (same doctrine as Parcel.extent_ref): voxel chunk ref, survey ref,
    # or empty for worlds with no navigable surface at all.
    extent_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pack_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # the pack that installed this row (§15.4); NULL = platform/legacy
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    parcels: Mapped[list["Parcel"]] = relationship(
        "Parcel", back_populates="place", order_by="Parcel.created_at"
    )

    def __repr__(self) -> str:
        return f"<Place {self.kind} key={self.key!r}>"
