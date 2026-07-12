from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econ.api.deps import get_session, require_admin
from econ.api.schemas import ScriptCreate, ScriptRead, ScriptUpdate, ScriptValidateResult
from econ.lua_engine import LuaEngine
from econ.models import Entity, Script, User
from econ.models.script import ScriptType

router = APIRouter(prefix="/admin/scripts", tags=["scripts"])

_engine = LuaEngine()

_MOCK_CTX = {
    "entity": {"id": "mock-entity", "name": "Mock Entity", "entity_type": "individual", "is_monetary_authority": False},
    "accounts": [{"id": "mock-account", "currency": "USD", "balance": "1000.0000"}],
    "events": [],
    "state": {},
    # lets HOOK/VALIDATOR scripts that read ctx.op be dry-run too
    "op": {
        "type": "transfer",
        "entity_id": "mock-entity",
        "from_account_id": "mock-account",
        "to_account_id": "mock-account-2",
        "amount": "100.0000",
        "currency": "USD",
        "reference": "mock",
        "transaction_ids": ["mock-tx-1", "mock-tx-2"],
    },
}


@router.get("", response_model=list[ScriptRead])
def list_scripts(
    script_type: ScriptType | None = None,
    is_active: bool | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    q = session.query(Script)
    if script_type is not None:
        q = q.filter_by(script_type=script_type)
    if is_active is not None:
        q = q.filter_by(is_active=is_active)
    return q.all()


@router.post("", response_model=ScriptRead, status_code=201)
def create_script(
    body: ScriptCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    if body.entity_id is not None and session.get(Entity, body.entity_id) is None:
        raise HTTPException(status_code=400, detail="Entity not found")
    script = Script(
        name=body.name,
        description=body.description,
        script_type=body.script_type,
        source=body.source,
        timeout_ms=body.timeout_ms,
        entity_id=body.entity_id,
    )
    session.add(script)
    session.commit()
    session.refresh(script)
    return script


@router.get("/{script_id}", response_model=ScriptRead)
def get_script(
    script_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    script = session.get(Script, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    return script


@router.patch("/{script_id}", response_model=ScriptRead)
def update_script(
    script_id: str,
    body: ScriptUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    script = session.get(Script, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    if body.name is not None:
        script.name = body.name
    if body.description is not None:
        script.description = body.description
    if body.source is not None:
        script.source = body.source
    if body.is_active is not None:
        script.is_active = body.is_active
    if body.timeout_ms is not None:
        script.timeout_ms = body.timeout_ms
    if body.entity_id is not None:
        if session.get(Entity, body.entity_id) is None:
            raise HTTPException(status_code=400, detail="Entity not found")
        script.entity_id = body.entity_id
    session.commit()
    session.refresh(script)
    return script


@router.delete("/{script_id}", status_code=204)
def delete_script(
    script_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    script = session.get(Script, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    session.delete(script)
    session.commit()


@router.post("/{script_id}/validate", response_model=ScriptValidateResult)
def validate_script(
    script_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """
    Dry-run the script against a mock ctx. Returns any syntax/runtime errors
    and the intents the script would have submitted (without executing them).
    """
    script = session.get(Script, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")

    result = _engine.run(script.source, _MOCK_CTX, timeout_ms=script.timeout_ms)

    return ScriptValidateResult(
        ok=result.error is None,
        error=result.error,
        intents=[asdict(i) for i in result.intents],
        return_value=_jsonable(result.return_value),
    )


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # e.g. a Lua function object
