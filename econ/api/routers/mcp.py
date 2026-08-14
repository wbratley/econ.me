"""The MCP endpoint -- the agent wire protocol (Model Context Protocol).

A minimal, **stateless** MCP server mounted at ``POST /mcp`` speaking the
Streamable HTTP transport: the client POSTs JSON-RPC 2.0 messages, the
server replies with plain ``application/json`` (no SSE stream -- the server
has nothing to push; rounds are resolved by the operator, not streamed).
``GET /mcp`` is 405 per the transport spec. No session state is kept
(``Mcp-Session-Id`` is unnecessary when every call is self-contained).

Why hand-rolled instead of the official ``mcp`` SDK: the SDK is not a
dependency of this project, and everything this server needs of the protocol
is three request methods (``initialize``, ``tools/list``, ``tools/call``)
plus notification swallowing. The tool surface itself lives in
``econ/api/mcp_tools.py``.

**Auth is the existing bearer scheme** (``get_current_user``): MCP clients
send ``Authorization: Bearer <token>`` exactly like REST players, and land
in the same dependency -- same user, same ownership gates, same admin rules.

Error mapping (JSON-RPC codes):
  * -32700  unparseable JSON
  * -32600  not a valid request / notification
  * -32601  unknown method
  * -32602  invalid params (incl. unknown tool name -- per the MCP spec)
  * tool-level failures (not-your-entity, bad source, fixed entity...) are
    NOT protocol errors: they come back as normal results with ``isError``.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session
from econ.api.mcp_tools import TOOL_HANDLERS, TOOLS, ToolError
from econengine.models import User

router = APIRouter(tags=["mcp"])

SERVER_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}

_JSONRPC_ERROR_PARSE = -32700
_JSONRPC_ERROR_REQUEST = -32600
_JSONRPC_ERROR_METHOD = -32601
_JSONRPC_ERROR_PARAMS = -32602
_JSONRPC_ERROR_INTERNAL = -32603


def _rpc_result(id_: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _initialize(id_: Any, params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = (
        requested if requested in SUPPORTED_PROTOCOL_VERSIONS
        else SERVER_PROTOCOL_VERSION
    )
    return _rpc_result(id_, {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "econ.me", "version": "1.0.0"},
    })


def _tools_list(id_: Any) -> dict:
    return _rpc_result(id_, {
        "tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            }
            for t in TOOLS
        ]
    })


def _tools_call(id_: Any, params: dict, session: Session, user: User) -> dict:
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOL_HANDLERS:
        return _rpc_error(id_, _JSONRPC_ERROR_PARAMS, f"Unknown tool: {name!r}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _rpc_error(id_, _JSONRPC_ERROR_PARAMS, "arguments must be an object")
    try:
        result = TOOL_HANDLERS[name](session, user, arguments)
        payload = json.dumps(result, indent=2, default=str)
    except ToolError as exc:
        # Well-formed call, game-level failure: report as a tool result.
        return _rpc_result(id_, {
            "content": [{"type": "text", "text": str(exc)}],
            "isError": True,
        })
    except Exception as exc:  # noqa: BLE001 -- surface as tool error, not a crash
        return _rpc_result(id_, {
            "content": [{"type": "text", "text": f"internal error: {exc}"}],
            "isError": True,
        })
    return _rpc_result(id_, {
        "content": [{"type": "text", "text": payload}],
    })


def _dispatch(message: dict, session: Session, user: User) -> dict | None:
    """Handle one JSON-RPC message. Returns a response object for requests,
    None for notifications (the transport answers 202)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc_error(None, _JSONRPC_ERROR_REQUEST, "Invalid JSON-RPC 2.0 message")
    id_ = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(method, str):
        return _rpc_error(id_, _JSONRPC_ERROR_REQUEST, "Missing method")

    is_notification = id_ is None
    if is_notification:
        # notifications (e.g. "notifications/initialized") need no reply
        return None

    if method == "initialize":
        return _initialize(id_, params if isinstance(params, dict) else {})
    if method == "ping":
        return _rpc_result(id_, {})
    if method == "tools/list":
        return _tools_list(id_)
    if method == "tools/call":
        return _tools_call(id_, params if isinstance(params, dict) else {}, session, user)
    return _rpc_error(id_, _JSONRPC_ERROR_METHOD, f"Unknown method: {method!r}")


@router.post("/mcp")
async def mcp_post(
    request: Request,
    session: Session = Depends(get_session),
    _: User = Depends(get_current_user),
):
    """The MCP Streamable HTTP endpoint (JSON responses, stateless).

    Accepts a single JSON-RPC message or a batch array (batches predate the
    2025-06-18 spec; supporting them costs three lines). Notifications are
    answered 202 with an empty body, per the transport."""
    raw = await request.body()
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return Response(
            content=json.dumps(_rpc_error(None, _JSONRPC_ERROR_PARSE, "Parse error")),
            media_type="application/json",
            status_code=400,
        )

    if isinstance(message, list):
        responses = []
        for item in message:
            resp = _dispatch(item, session, _) if isinstance(item, dict) else _rpc_error(
                None, _JSONRPC_ERROR_REQUEST, "Invalid JSON-RPC 2.0 message",
            )
            if resp is not None:
                responses.append(resp)
        if not responses:  # batch of notifications only
            return Response(status_code=202)
        return Response(
            content=json.dumps(responses), media_type="application/json",
        )

    response = _dispatch(message, session, _)
    if response is None:  # notification
        return Response(status_code=202)
    return Response(content=json.dumps(response), media_type="application/json")


@router.get("/mcp")
def mcp_get(_: User = Depends(get_current_user)):
    """Streamable HTTP transport: GET (server-initiated stream) is refused --
    this server has nothing to push."""
    return Response(status_code=405)
