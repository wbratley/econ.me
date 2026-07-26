from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class WorldSetting(Base):
    """World-level votable data as key → JSON value (the estate rule lives
    here). Governance writes these; the engine only reads them — decision
    rules are data, effect mechanisms are engine."""

    __tablename__ = "world_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<WorldSetting {self.key}={self.value!r}>"
