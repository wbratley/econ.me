import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class SpatialEdge(Base):
    """One road on the world's map (docs/spatial.md S3): a weighted,
    mode-tagged connection between two places. The topology half of the
    spatial layer — distance in the engine is always ticks-through-edges,
    never meters (Fork 1: opaque refs, no coordinates).

    An edge declares how travel happens along it: ``mode`` (uppercase tag
    — WALK, RAFT, RIDE; the pack authors a TRAVEL_{MODE} recipe whose
    normal requirements gate it, so a vehicle is a requirement, not
    mechanism) and ``cost_ticks`` (the hop's duration — one Process per
    hop, completes_tick = start + cost). ``bidirectional`` (default) lets
    the road be walked both ways; a one-way edge only goes from → to.

    Edges are content rows like places: installed at genesis, keyed to a
    pack, counted and pinned by the manifest. A world with no edges has
    no topology — every journey is refused with a readable reason, and
    everything else runs exactly as before."""

    __tablename__ = "spatial_edges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    from_place_id: Mapped[str] = mapped_column(String(36), ForeignKey("places.id"), nullable=False)
    to_place_id: Mapped[str] = mapped_column(String(36), ForeignKey("places.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)  # uppercase tag: WALK, RAFT, RIDE...
    cost_ticks: Mapped[int] = mapped_column(Integer, nullable=False)  # >= 1: the hop's duration
    bidirectional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # fork-copy scoping (S6): which region of the map this road belongs to
    region_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    pack_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # the pack that installed this row (§15.4); NULL = platform/legacy
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    from_place: Mapped["Place"] = relationship("Place", foreign_keys=[from_place_id])
    to_place: Mapped["Place"] = relationship("Place", foreign_keys=[to_place_id])

    def __repr__(self) -> str:
        arrow = "<->" if self.bidirectional else "->"
        return f"<SpatialEdge {self.from_place_id[:8]}{arrow}{self.to_place_id[:8]} {self.mode} {self.cost_ticks}t>"
