"""The estate escrow -- the dead hand holding the estate for the heir.

Succession's cost model (the generational game, docs/design.md §5):
incapacity may BURN the estate (today's default), HAND it to a
designated heir, or -- policy ``escrow`` -- hold it for a limited
window against the owner's NEXT founder. The goods leave the world at
death (holdings zeroed, money out of supply) and re-enter, decayed by
the wait, only if an heir joins inside the window:

    delivered = escrowed * (1 - decay) ** (join_tick - created_tick)

The decay is the anti-rebirth tax: dying to reset one's vitals must
cost more capital than the reset is worth, or famine becomes a
retirement plan. Both knobs (window, decay) are votable data in the
``estate.escrow`` WorldSetting; this table is just the ledger row the
engine's estate pass writes and the join path reads.

One row per owner: a second death inside the window MERGES into the
open escrow (quantities sum, the window extends to the later expiry,
the camp moves to the later death) -- the estate of a dynasty is one
pot, however many bodies it burned.
"""
import uuid

from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class EstateEscrow(Base):
    __tablename__ = "estate_escrows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # the dynasty (users.id): the delivery key -- the NEXT founder under
    # this owner inherits. Platform user id, not engine entity id.
    owner_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    # provenance: whose death filled the pot (the FIRST body if merged)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    # exact-decimal strings throughout (the JSON rule): {"WOOD": "4.4366"}
    goods: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    money: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # parcels reverted to unclaimed at death; delivery re-claims whichever
    # are STILL unclaimed (the claim race is part of the cost)
    parcels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # the camp: where the heir wakes (the dead entity's location)
    place_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover -- debug aid
        return (f"<EstateEscrow owner={self.owner_id} "
                f"t{self.created_tick}..t{self.expires_tick}>")
