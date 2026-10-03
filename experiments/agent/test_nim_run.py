"""The MCP call tracer (nim_run.McpCallLog / _traced / _abbrev): every
round trip a seat makes lands as one JSON line — the timestamped proof
behind AGENT-API.md §4's choreography (run 52's forensics rebuilt this
timeline by inference; the next run reads it)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from experiments.agent.nim_run import McpCallLog, _abbrev, _traced


def _lines(path) -> list[dict]:
    return [json.loads(line) for line
            in Path(path).read_text().splitlines()]


def test_traced_transport_logs_every_call(tmp_path):
    log = McpCallLog(tmp_path / "mcp-calls.jsonl")

    def stub(method, params):
        if params.get("name") == "boom":
            raise RuntimeError("MCP tools/call: {'code': -32000}")
        return {"content": [{"type": "text", "text": '{"rows": []}'}]}

    traced = _traced(stub, "House One", log)
    out = traced("tools/call", {"name": "round_state", "arguments": {}})
    assert out["content"][0]["text"] == '{"rows": []}'
    try:
        traced("tools/call", {"name": "boom", "arguments": {"entity_id": "e"}})
    except RuntimeError:
        pass
    else:
        raise AssertionError("the transport's error must propagate")

    lines = _lines(tmp_path / "mcp-calls.jsonl")
    assert [l["tool"] for l in lines] == ["round_state", "boom"]

    ok = lines[0]
    assert ok["seat"] == "House One" and ok["method"] == "tools/call"
    assert ok["ok"] is True and ok["error"] is None and ok["args"] == {}
    assert ok["elapsed_s"] >= 0 and ok["ts"]
    assert ok["result_chars"] > 0 and "text" in ok["result_head"]

    bad = lines[1]
    assert bad["ok"] is False and "-32000" in bad["error"]
    assert bad["args"] == {"entity_id": "e"} and bad["result_chars"] == 0


def test_call_log_interleaves_safely(tmp_path):
    """Run 53 runs one authoring thread per dynasty over the same log —
    two seats writing concurrently must never corrupt a line."""
    import threading

    log = McpCallLog(tmp_path / "mcp-calls.jsonl")

    def stub(method, params):
        return {"content": [{"type": "text", "text": "{}"}]}

    def hammer(seat):
        t = _traced(stub, seat, log)
        for _ in range(200):
            t("tools/call", {"name": "round_state", "arguments": {}})

    threads = [threading.Thread(target=hammer, args=(f"House {i}",))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = _lines(tmp_path / "mcp-calls.jsonl")   # every line parses
    assert len(lines) == 800


def test_abbrev_truncates_huge_sources():
    src = "local x = 1\n" + "-- filler\n" * 2000
    a = _abbrev({"entity_id": "e1", "source": src, "last_ticks": 50})
    assert len(a["source"]) < 400                      # the line stays small
    assert a["source"].endswith(                       # but joinable to the
        hashlib.sha1(src.encode()).hexdigest()[:16] + "]")   # journal's sha
    assert a["last_ticks"] == 50 and a["entity_id"] == "e1"
    assert _abbrev({}) == {} and _abbrev("short") == "short"
    nested = _abbrev({"deep": [{"also": "x" * 900}]})
    assert "sha1:" in nested["deep"][0]["also"]
