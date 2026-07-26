import uuid
import enum
from datetime import datetime, timezone
from sqlalchemy import Boolean, Integer, String, Text, DateTime, JSON, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base


class ScriptType(enum.Enum):
    POLICY     = "policy"      # country-level rules, fired on hook events
    BEHAVIOUR  = "behaviour"   # entity-level, fired on tick
    HOOK       = "hook"        # before/after specific service operations
    VALIDATOR  = "validator"   # pure allow/deny, no side effects


class Script(Base):
    __tablename__ = "scripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    script_type: Mapped[ScriptType] = mapped_column(SAEnum(ScriptType), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # BEHAVIOUR scripts run as this entity each tick; global types leave it NULL
    entity_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("entities.id"), nullable=True)
    # persistent ctx.state, updated after each successful run
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    entity: Mapped["Entity | None"] = relationship("Entity")

    def __repr__(self) -> str:
        return f"<Script id={self.id} name={self.name!r} type={self.script_type.value}>"
