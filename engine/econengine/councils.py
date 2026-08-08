"""Council registers — the membership data for council/weighted governance.

A council is a named body whose members vote on proposals. Unlike the
*citizen* electorate (computed from entity type) or the *share* electorate
(computed from the holding register), a council's membership is *authored
policy data*: who sits on the senate is a political fact, not something the
engine can derive from existing state. So a council register is a single
``WorldSetting`` row — the form-specific data the ``council`` and
``weighted`` weight models resolve against (docs/actors.md, "Forms of
government are data").

This module is pure data access — the constitutional equivalent of
``fiscal.py`` and the membership analogue of the holding register. It holds
no authority: setting a register is an admin/platform act today (mirroring
``PUT /admin/estate-rule``); in a self-governing world the membership is
itself policy the platform drives. The engine only *reads* it, so the two
weight models are one register plus a weight function:

  - ``council:{name}``  — every member, weight 1 each (equal council);
  - ``weighted:{name}`` — every member, weight = declared per-member weight
    (a weighted council, which also subsumes a representative chamber:
    set each MP's weight to their constituency size).

Register format: a JSON object ``{member_entity_id: weight_str}``. An
equal-weight council simply lists its members with any weight (the
``council`` model ignores it); a weighted council declares real weights
that the ``weighted`` model honours. Replacing the register wholesale
(``set_register``) makes membership changes atomic and auditable.
"""

from decimal import Decimal
from typing import Union

from sqlalchemy.orm import Session

from .models import WorldSetting

#: The world-setting key prefix for council registers. A register for the
#: council named ``senate`` lives under ``council:senate``.
COUNCIL_PREFIX = "council:"

#: A register member's weight resolves to 1 when absent or unparseable, so a
#: council authored as a bare membership list (weights "1" or omitted) still
#: resolves under the ``weighted`` model without surprise.
DEFAULT_MEMBER_WEIGHT = Decimal(1)


def _key(name: str) -> str:
    return COUNCIL_PREFIX + name


def get_register(session: Session, name: str) -> dict[str, str]:
    """The membership dict ``{member_entity_id: weight_str}`` for council
    ``name``, or ``{}`` if no register exists.

    Returns the raw stored strings (weights are form-specific; the weight
    models parse them). An empty dict means the council has no members.
    """
    setting = session.get(WorldSetting, _key(name))
    if setting is None or not isinstance(setting.value, dict):
        return {}
    # store/return member ids as strings, weights as strings (stringly typed)
    return {str(k): str(v) for k, v in setting.value.items()}


def member_weight(session: Session, name: str, entity_id: str) -> Decimal:
    """One member's *declared* weight from council ``name``'s register.

    Returns ``DEFAULT_MEMBER_WEIGHT`` (1) if the member is in the register
    but has no usable weight, and 0 if they are not a member at all. The
    ``weighted`` weight model routes here; the ``council`` model ignores
    the value (everyone is 1). Membership/activeness is enforced by the
    weight model, not here.
    """
    register = get_register(session, name)
    if entity_id not in register:
        return Decimal(0)
    try:
        return Decimal(register[entity_id])
    except (ArithmeticError, ValueError):
        return DEFAULT_MEMBER_WEIGHT


def set_register(
    session: Session,
    name: str,
    members: Union[list[str], dict[str, object]],
) -> WorldSetting:
    """Replace council ``name``'s membership wholesale.

    ``members`` may be a list of entity ids (an equal-weight council — every
    member gets weight 1, which the ``council`` model honours and the
    ``weighted`` model treats as uniform) or a mapping
    ``{entity_id: weight}`` (a weighted council). The stored form is always
    a ``{member_id: weight_str}`` object, so the two weight models read one
    register. Replacing wholesale makes membership changes atomic.

    No authority check here — this is data access. Seeding is an admin act
    today (``PUT /admin/councils/{name}``); direct callers (tests, admin
    tooling) may use this to plant a council.
    """
    if isinstance(members, dict):
        normalised = {str(k): str(v) for k, v in members.items()}
    elif isinstance(members, list):
        normalised = {str(eid): "1" for eid in members}
    else:
        raise ValueError("members must be a list of entity ids or a {id: weight} map")
    if not normalised:
        raise ValueError("a council must have at least one member")
    key = _key(name)
    setting = session.get(WorldSetting, key)
    if setting is None:
        setting = WorldSetting(key=key, value=normalised)
        session.add(setting)
    else:
        setting.value = normalised
    session.flush()
    return setting


def delete_register(session: Session, name: str) -> bool:
    """Remove council ``name``'s register. Returns whether a row was deleted
    (False if the council never existed)."""
    setting = session.get(WorldSetting, _key(name))
    if setting is None:
        return False
    session.delete(setting)
    session.flush()
    return True
