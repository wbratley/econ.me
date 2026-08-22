"""Witness delivery rows (game.md 15.6): who perceived which event.

Derived data recorded at emission time -- never part of ``events_hash``.
See ``econengine/witness.py`` for the delivery rule.
"""
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EventObserver(Base):
    """One (tick, event index) delivered to one entity. The composite key
    IS the fact: no row twice, no meaning beyond presence."""

    __tablename__ = "event_observers"

    tick_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    observer_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    def __repr__(self) -> str:
        return (f"<EventObserver tick={self.tick_number} "
                f"event={self.event_index} observer={self.observer_id}>")
