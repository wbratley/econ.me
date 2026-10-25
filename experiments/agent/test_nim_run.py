"""The MCP call tracer (nim_run.McpCallLog / _traced / _abbrev): every
round trip a seat makes lands as one JSON line — the timestamped proof
behind AGENT-API.md §4's choreography (run 52's forensics rebuilt this
timeline by inference; the next run reads it)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.agent.nim_run import (McpCallLog, _abbrev, _liveness_gate,
                                       _parse_epoch_spec, _traced,
                                       create_browser_user)


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


def test_create_browser_user_is_reusable_and_mints_an_account_token(tmp_path):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from econ.api.auth import decode_token
    from econengine.models import User

    db_path = tmp_path / "world.db"
    first_id, first_token = create_browser_user(db_path, "Ice Browser")
    second_id, second_token = create_browser_user(db_path, "Ice Browser")

    assert first_id == second_id == "u-browser-ice-browser"
    assert decode_token(first_token)["sub"] == first_id
    assert decode_token(second_token)["sub"] == first_id
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with Session(engine) as session:
            users = session.execute(
                select(User).where(User.id == first_id)).scalars().all()
    finally:
        engine.dispose()
    assert len(users) == 1
    assert users[0].name == "Ice Browser" and users[0].is_admin is False


# ===========================================================================
# The liveness gate: a dead model must never inherit the scenario starter
# silently (run 54's Harald rode a 410'd slug for three rounds while his
# journal just said "model failure")
# ===========================================================================

class _ProbeModel:
    """The model shape the gate probes: (ok, detail, status) per call.
    A list of results is popped FIFO; the last one sticks."""

    def __init__(self, results):
        self._results = list(results)
        self.probes = 0

    def probe(self, timeout=60.0):
        self.probes += 1
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def test_liveness_gate_refuses_a_410_model():
    """A slug that 410s is a configuration fact (gone/misspelled), not a
    busy endpoint: the gate refuses the start on the first probe —
    no retry, no backoff, the message says why."""
    dead = _ProbeModel([(False, 'HTTP 410: b\'{"detail": "Gone"}\'', 410)])
    try:
        _liveness_gate([("House Harald", dead, "moonshotai/kimi-k3")])
    except SystemExit as exc:
        assert "REFUSING TO START" in str(exc)
        assert "HTTP 410" in str(exc)
        assert "scenario starter" in str(exc)
    else:
        raise AssertionError("the 410 slug must refuse the start")
    assert dead.probes == 1        # a configuration fact is not retried


def test_liveness_gate_retries_a_busy_endpoint_then_passes():
    """A 503 (overload) is a busy endpoint: back off and retry until the
    model answers — the run 54 nemotron seat's actual state was 503-
    alive, and a run that refused on 503 would never have started."""
    busy = _ProbeModel([
        (False, 'HTTP 503: b\'{"error": {"message": "overloaded"}}\'', 503),
        (True, "ok", 200),
    ])
    _liveness_gate([("House Harald", busy,
                    "nvidia/nemotron-3-ultra-550b-a55b")],
                  budget_s=60, first_backoff_s=0.05)
    assert busy.probes == 2


def test_liveness_gate_budget_exhaustion_refuses():
    """A model that is busy for the whole budget is still a model the
    seat cannot reach: the start is refused when the budget runs out,
    with the last probe's detail — not silently on the starter."""
    busy = _ProbeModel([(False, "HTTP 503: overloaded", 503)])
    try:
        _liveness_gate([("House Harald", busy, "some-slug")],
                      budget_s=0.05, first_backoff_s=0.05)
    except SystemExit as exc:
        assert "REFUSING TO START" in str(exc)
        assert "still unavailable" in str(exc)
        assert "503" in str(exc)
    else:
        raise AssertionError("budget exhaustion must refuse the start")


def test_liveness_gate_skips_unprobed_seats():
    """Scripted and live-file seats have no endpoint — no `probe`
    attribute, nothing to gate."""

    class _NoProbe:
        name = "scripted"

    _liveness_gate([("House One", _NoProbe(), "scripted:one.jsonl")])


# ===========================================================================
# --epoch CODE:VALUE: the parser splits on the first colon and rides the
# value on the code's own param key; normalise_condition (inside
# start_epoch) does the validating, so the parser only strips the code
# and leaves the value to be re-cased / re-typed there.
# ===========================================================================

def test_parse_epoch_spec_maps_each_code_to_its_param_key():
    assert _parse_epoch_spec("innovate:FIRE_HARDENING") == \
        ("innovate", {"technology": "FIRE_HARDENING"})
    assert _parse_epoch_spec("accumulate:50") == \
        ("accumulate", {"threshold": "50"})
    assert _parse_epoch_spec("endure:500") == ("endure", {"ticks": "500"})
    assert _parse_epoch_spec("grow:2") == ("grow", {"threshold": "2"})
    # code is case/whitespace-tolerant; the value is stripped but NOT
    # re-cased (normalise_condition uppercases the technology itself)
    assert _parse_epoch_spec("  INNOVATE:  fire_hardening ") == \
        ("innovate", {"technology": "fire_hardening"})


def test_parse_epoch_spec_refuses_bad_specs():
    for bad in ("", "innovate", "innovate:", ":FIRE_HARDENING",
               "bogus:FIRE_HARDENING", "accumulate"):
        with pytest.raises(ValueError):
            _parse_epoch_spec(bad)
