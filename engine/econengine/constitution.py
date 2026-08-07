"""
The constitution — votable voting-system params in ``world_settings``.

This is the constitutional tier of the mechanism/data/policy split
(`docs/actors.md` step 4b / 4a-4). Above ordinary law (POLICY/BEHAVIOUR
scripts and the fiscal-policy dict) sits the *constitution*: the VALIDATOR
scripts (the constraints ordinary law must clear) and the voting-system
parameters (the threshold and quorum a constitutional amendment itself
must clear). Both are amendable only through a constitutional proposal —
``set_validator`` and ``set_constitution``, both gated by the
``amend_constitution`` capability and bound by a supermajority.

This module is the *data* access for the voting-system half — the
constitutional equivalent of ``fiscal.py``. It holds a single
``WorldSetting`` row keyed ``constitution`` whose value is a JSON object
of the supermajority floor. Authority is enforced one layer up, in
``services.set_constitution`` (the ``amend_constitution`` capability),
so these helpers are pure data access — direct callers (tests, admin
seeding) may use them to plant a constitution.

Defaults apply when no row exists (or a param is absent), so there is
always a constitution in force: a two-thirds supermajority threshold and
no quorum floor — the conventional bar for amending a charter.
"""

from decimal import Decimal
from sqlalchemy.orm import Session

from .models import WorldSetting

#: The single world-setting key holding the voting-system floor as a JSON
#: object. Absent ⇒ the DEFAULT_CONSTITUTION below.
CONSTITUTION_KEY = "constitution"

#: The constitution a world has until its citizens amend it. A two-thirds
#: supermajority of cast weight, no quorum floor — the traditional bar to
#: amend a founding document. Stored as strings because intent params and
#: every other votable value are stringly typed (exact decimals for Lua).
DEFAULT_CONSTITUTION = {
    "supermajority_threshold": "0.67",
    "supermajority_quorum": "0",
}


def get_constitution(session: Session) -> dict:
    """The constitution dict, with defaults for any missing param.

    Always returns a full dict (threshold + quorum) so callers can index
    without guarding for absence — there is always a constitution in force.
    """
    setting = session.get(WorldSetting, CONSTITUTION_KEY)
    stored = setting.value if (setting is not None and isinstance(setting.value, dict)) else {}
    return {**DEFAULT_CONSTITUTION, **stored}


def set_constitution(session: Session, params: dict) -> WorldSetting:
    """Replace the constitution dict wholesale.

    No authority check here — this is data access. The privileged action
    (the ``amend_constitution`` capability + a constitutional vote) is
    ``services.set_constitution``; direct callers (tests, admin tooling)
    may use this to seed a world. Unknown keys are kept verbatim so future
    params ride without a migration, but the two floor params always
    resolve through the defaults above when read.
    """
    if not isinstance(params, dict):
        raise ValueError("constitution must be a JSON object")
    # merge over defaults so a partial amendment (one param) doesn't drop
    # the other; an explicit null/absent key keeps its default.
    merged = {**DEFAULT_CONSTITUTION, **{k: v for k, v in params.items() if v is not None}}
    setting = session.get(WorldSetting, CONSTITUTION_KEY)
    if setting is None:
        setting = WorldSetting(key=CONSTITUTION_KEY, value=merged)
        session.add(setting)
    else:
        setting.value = merged
    session.flush()
    return setting


def supermajority_floor(session: Session) -> tuple[Decimal, Decimal]:
    """The (threshold, quorum) floor a constitutional enactment must clear.

    Returns Decimals for direct arithmetic against the tally in
    ``enact_proposal``.
    """
    c = get_constitution(session)
    return Decimal(c["supermajority_threshold"]), Decimal(c["supermajority_quorum"])
