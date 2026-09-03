import uuid
import enum
from sqlalchemy import Boolean, Integer, String, Enum as SAEnum, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base
from .. import capabilities as _capabilities


class EntityType(enum.Enum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"
    BANK = "bank"
    GOVERNMENT = "government"


class EntityStatus(enum.Enum):
    """Engine lifecycle state. INCAPACITATED entities take no part in any
    tick pass and cannot act; permanent death is world policy layered on
    top (docs/design.md § conditions)."""

    ACTIVE = "active"
    INCAPACITATED = "incapacitated"


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(SAEnum(EntityType), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    is_monetary_authority: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Privilege set for non-self-directed action (tax, seizure, policy).
    # See `docs/actors.md` Fork 2 and `engine/econengine/capabilities.py`.
    # The legacy `is_monetary_authority` flag implies the monetary
    # capability, so nothing already created loses access; new privileged
    # actions are granted via this column directly.
    capabilities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[EntityStatus] = mapped_column(
        SAEnum(EntityStatus), nullable=False, default=EntityStatus.ACTIVE
    )
    incapacitated_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Tick the entity came into being; age = ctx.tick - birth_tick (Step 6,
    # docs/actors.md). Set once at creation, never mutated -- age is
    # unforgeable the way holdings are (a script cannot change its birth
    # tick any more than it can change its body). NULL means the entity
    # predates age-tracking; ``ctx.query.age()`` reads nil for it.
    birth_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Provenance -- the parents of this entity (Step 6c, docs/actors.md).
    # A generic list of entity ids stamped once by ``spawn_entity`` and
    # never mutated: lineage must be authoritative for inheritance
    # (``heir_id``) and consanguinity rules, so it cannot live in
    # scribbleable script state. The engine STORES the list; it does NOT
    # interpret it -- two-parent biology, one-parent manufacturing, and
    # zero-parent spontaneous generation are just different-length lists.
    # NULL means the entity predates spawn-tracking (made at world setup).
    parents: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # The age (in ticks) at which this entity dies of old age -- the
    # invariant mortality floor of Step 6d (docs/actors.md). The engine's
    # end-of-tick incapacity pass deactivates the entity once
    # ``age = tick - birth_tick`` reaches this and applies the estate
    # rule, firing ``entity_incapacitated`` with ``condition: "age"``.
    # NULL means *immortal* (the default): nothing already built ever
    # dies of old age, and the feature is opt-in. It is per-entity data,
    # not a votable WorldSetting -- the roadmap's "not votable per tick"
    # makes a votable lifespan self-defeating. Stamped once at
    # spawn/creation; there is no engine setter, so it is immutable the
    # way ``birth_tick`` and ``parents`` are (a future ``set_lifespan``
    # intent under a capability is the reserved escape valve). The world
    # adjusts the *regime* by amending the governed spawn POLICY; the
    # *dynamic* face of mortality stays the shipped condition pass.
    lifespan: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heir_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id"), nullable=True
    )  # estate recipient under the "heir" rule; unset falls back to burn
    # The immutable-tier mark of the three-tier control model
    # (``docs/game.md`` §4, §6). True means this entity's BEHAVIOUR is
    # operator-set world-physics: both the autonomy path
    # (``set_entity_behaviour``) and the legislation path (``set_script``)
    # refuse to change it for the epoch. NPC labourers and environment
    # actors are stamped True at content time; everything else defaults
    # False. It is data the operator sets (admin API / scenario), never a
    # governed mutation -- making a fixed entity editable would be a
    # policy decision layered above the engine, not inside it.
    is_fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Where the entity stands on the world's map (docs/spatial.md S1):
    # an opaque place ref, never a coordinate. NULL = unplaced — the
    # legacy citizen (Fork 6): abstract worlds run identically to today,
    # subject to no spatial gate. Written only by ``places.move_entity``
    # (genesis and tests) until travel arrives in S3 to write it too.
    location_place_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("places.id"), nullable=True
    )

    accounts: Mapped[list["Account"]] = relationship("Account", back_populates="entity")
    owner: Mapped["User | None"] = relationship("User", back_populates="entities")
    place: Mapped["Place | None"] = relationship("Place")

    def has_capability(self, name: str) -> bool:
        """True if this entity holds capability `name`.

        The single check site for actor privileges. `is_monetary_authority`
        is treated as a legacy alias for the monetary capability so existing
        worlds keep working; everything else reads the `capabilities` set.
        """
        if name in (self.capabilities or []):
            return True
        if name == _capabilities.MONETARY_AUTHORITY and self.is_monetary_authority:
            return True
        return False

    def __repr__(self) -> str:
        return f"<Entity id={self.id} name={self.name!r} type={self.entity_type.value}>"
