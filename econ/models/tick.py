import uuid
from datetime import datetime, timezone
from sqlalchemy import Integer, String, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class Tick(Base):
    __tablename__ = "ticks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # sha256 of the canonical JSON of `events` — the entropy source for the
    # next tick's outcome rolls (see econ/rng.py), and an audit commitment
    events_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<Tick number={self.number} events={len(self.events or [])}>"
