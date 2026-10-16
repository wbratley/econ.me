"""Player onboarding config (docs/game.md §6, §12.6; Phase 1).

The join flow is **platform orchestration over engine primitives** -- it
creates no new mechanism. What a new player is *endowed* with (the starter
BEHAVIOUR source, the genesis money, the currency, and the seat: holdings,
stats, a parcel, a place) is **world content (data)**, not mechanism. So it
lives in a WorldSetting (``join.config``)
that the operator / content-pack bootstrap writes, and the ``POST /join``
endpoint reads. The platform never hardcodes a starter template: it treats
"what a new player starts with" as data, the way the estate rule and the
compute budget are data.

This deliberately keeps the content pack out of the platform code. A world
is bootstrapped once (goods/tech/recipes/needs/markets + this setting);
players then join it repeatedly. The starter source only makes sense against
a world whose content it references, which is exactly the content/mechanism
separation ``design.md §2`` draws.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from econengine import combat, markets, parcels, places
from econengine.models import Entity, WorldSetting

#: Single WorldSetting holding the join-time founder package. A dict so the
#: operator can set any subset of fields without clobbering the rest.
JOIN_CONFIG_KEY = "join.config"

DEFAULT_CURRENCY = "USD"
#: Zero by default: join never silently mints money. A world that wants
#: founders funded sets an endowment at bootstrap time.
DEFAULT_ENDOWMENT = Decimal("0")


def get_join_config(session: Session) -> dict[str, Any]:
    """Read the join-time config, filling defaults for anything unset.

    Returns a normalised dict:
      * ``endowment``    -- Decimal (never None)
      * ``currency``     -- upper-case str
      * ``starter_behaviour`` -- str | None (None = no starter configured)
      * ``holdings``     -- dict of symbol -> exact-decimal string
      * ``stats``        -- dict of stat name -> exact-decimal string
      * ``parcel``       -- {"type", "name", "place"} | None
      * ``place``        -- str | None (the place key the founder wakes at)
    """
    row = session.get(WorldSetting, JOIN_CONFIG_KEY)
    raw = dict(row.value) if row else {}
    return {
        "endowment": Decimal(str(raw.get("endowment", DEFAULT_ENDOWMENT))),
        "currency": str(raw.get("currency", DEFAULT_CURRENCY)).upper(),
        "starter_behaviour": raw.get("starter_behaviour"),
        "holdings": {str(k): str(v)
                     for k, v in (raw.get("holdings") or {}).items()},
        "stats": {str(k): str(v) for k, v in (raw.get("stats") or {}).items()},
        "parcel": dict(raw["parcel"]) if raw.get("parcel") else None,
        "place": raw.get("place"),
    }


def set_join_config(
    session: Session,
    *,
    endowment: Decimal | str | None = None,
    currency: str | None = None,
    starter_behaviour: str | None = None,
    holdings: dict | None = None,
    stats: dict | None = None,
    parcel: dict | None = None,
    place: str | None = None,
) -> dict[str, Any]:
    """Upsert the join config, **merging** provided fields (None = unchanged).

    Merge -- not replace -- so an operator can set the endowment without
    knowing the current starter, or rotate the starter without touching the
    money. Call repeatedly; each call updates only the fields it is given.
    """
    row = session.get(WorldSetting, JOIN_CONFIG_KEY)
    value = dict(row.value) if row else {}
    if endowment is not None:
        value["endowment"] = str(Decimal(str(endowment)))
    if currency is not None:
        value["currency"] = str(currency).upper()
    if starter_behaviour is not None:
        value["starter_behaviour"] = starter_behaviour
    if holdings is not None:
        value["holdings"] = {str(k): str(v) for k, v in holdings.items()}
    if stats is not None:
        value["stats"] = {str(k): str(v) for k, v in stats.items()}
    if parcel is not None:
        value["parcel"] = {str(k): str(v) for k, v in parcel.items()}
    if place is not None:
        value["place"] = str(place)
    if row is None:
        session.add(WorldSetting(key=JOIN_CONFIG_KEY, value=value))
    else:
        row.value = value
    session.flush()
    return get_join_config(session)


def apply_founder_seat(session: Session, entity: Entity, cfg: dict[str, Any]):
    """Endow the founder's **seat** from the join config: holdings, stats,
    a parcel, and the place it wakes at -- applied in the content pack's
    make-house order (holdings, stats, parcel, place; the caller creates
    the account first and installs the starter last).

    No new mechanism: each field lands through the engine's declared
    passes (``markets.adjust_holding``, ``combat.create_stat``,
    ``parcels.create_parcel``, ``places.move_entity``) exactly as a pack's
    ``make_house`` does. Every field is optional; a config with none set
    is a no-op, which is the bare founder of worlds that set none."""
    for symbol, qty in (cfg.get("holdings") or {}).items():
        markets.adjust_holding(session, entity, symbol, Decimal(str(qty)))
    for stat, qty in (cfg.get("stats") or {}).items():
        combat.create_stat(session, entity.id, stat, Decimal(str(qty)))
    parcel = cfg.get("parcel")
    if parcel:
        parcels.create_parcel(
            session, parcel["type"], name=parcel.get("name", ""),
            owner=entity, place=parcel.get("place"))
    if cfg.get("place"):
        places.move_entity(session, entity, cfg["place"])
