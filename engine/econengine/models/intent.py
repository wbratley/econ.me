from datetime import datetime, timezone
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class PendingIntent(Base):
    """A direct-action outbox row: an intent submitted by the entity's
    controller OUTSIDE any behaviour script (the API/test path — a machine
    client POSTing /intents, a seat's perform_action, a future human's
    client). Under the world clock these queue here between ticks and
    run_tick drains them into the same intent-resolution pass scripts
    feed — same resolver, same priority order, same per-tick budgets —
    so a direct say lands in the Tick's events where every script and
    seat sees it, instead of resolving into the void between ticks."""

    __tablename__ = "pending_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id"), nullable=False, index=True)
    intent_type: Mapped[str] = mapped_column(String(48), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"<PendingIntent {self.entity_id}:{self.intent_type}>"
