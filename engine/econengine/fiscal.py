"""
Fiscal policy — votable government data in ``world_settings``.

This is the *data* layer of the mechanism/data/policy split
(`docs/design.md` §2). The engine stores fiscal policy as a single
votable ``WorldSetting`` row; it does NOT interpret what the rates mean.
A government's POLICY script reads this dict (``ctx.query.fiscal_policy()``)
and turns it into ``ctx.action.levy(...)`` calls — that script is the
*policy*, ``services.levy`` (step 2) is the *mechanism*, and the dict
written here is the *data* citizens vote on (rates and schedules, not code).

Authority is enforced one layer up, in ``services.set_fiscal_policy`` (the
``set_fiscal_policy`` capability + a VALIDATOR veto), so these helpers are
pure data access — the fiscal equivalent of ``conditions.get_estate_rule``
/ ``set_estate_rule``. ``services`` imports this module; nothing here
imports ``services``.

The value is a JSON object the authority controls wholesale (replace
semantics). Keeping one structured key — rather than a sprawl of
``fiscal.*`` rows — mirrors the estate rule's single key and keeps a policy
change atomic and auditable.
"""

from sqlalchemy.orm import Session

from .models import WorldSetting

#: The single world-setting key holding the government's fiscal policy as a
#: JSON object. Absent ⇒ no fiscal policy (an empty dict).
FISCAL_POLICY_KEY = "fiscal_policy"


def get_fiscal_policy(session: Session) -> dict:
    """The fiscal-policy dict, or ``{}`` if none is set."""
    setting = session.get(WorldSetting, FISCAL_POLICY_KEY)
    if setting is None or not isinstance(setting.value, dict):
        return {}
    return dict(setting.value)


def set_fiscal_policy(session: Session, policy: dict) -> WorldSetting:
    """Replace the fiscal-policy dict wholesale.

    No authority check here — this is data access. The privileged action
    (capability + validator veto) is ``services.set_fiscal_policy``; direct
    callers (tests, admin tooling) may use this to seed a world.
    """
    if not isinstance(policy, dict):
        raise ValueError("fiscal policy must be a JSON object")
    setting = session.get(WorldSetting, FISCAL_POLICY_KEY)
    if setting is None:
        setting = WorldSetting(key=FISCAL_POLICY_KEY, value=policy)
        session.add(setting)
    else:
        setting.value = policy
    session.flush()
    return setting
