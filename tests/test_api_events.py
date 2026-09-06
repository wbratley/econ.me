"""Unit tests for econ.api.events -- the in-process pub/sub behind the
SSE round-event stream (M2a).

The two things that must be true: delivery is thread-safe (publish is
called from sync request handlers in FastAPI's threadpool; subscribers
are asyncio queues on the server's event loop), and publishing with no
subscribers is a free no-op (the common case in runs and tests).
"""

import asyncio
import threading

from econ.api import events


def _run(coro):
    return asyncio.run(coro)


def test_publish_reaches_subscriber_across_threads():
    """The production shape: subscribe on the loop, publish from another
    thread (a request handler), receive on the loop."""
    received = []

    async def main():
        q = events.subscribe()
        try:
            t = threading.Thread(
                target=lambda: events.publish("round_closed", {"round": 2}))
            t.start()
            item = await asyncio.wait_for(q.get(), timeout=5.0)
            received.append(item)
            t.join()
        finally:
            events.unsubscribe(q)

    _run(main())
    assert received == [("round_closed", {"round": 2})]


def test_unsubscribe_stops_delivery():
    received = []

    async def main():
        q = events.subscribe()
        events.unsubscribe(q)
        events.publish("readiness", {"ready": 1})
        try:
            item = await asyncio.wait_for(q.get(), timeout=0.2)
            received.append(item)
        except asyncio.TimeoutError:
            pass
        assert not received

    _run(main())


def test_publish_without_subscribers_is_free():
    events.publish("round_opened", {"round": 3})   # must not raise


def test_publish_round_closed_pair_includes_deadline(monkeypatch):
    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "900")
    seen = []
    events.subscribe  # noqa: B018 -- documented below
    # capture via a manual subscriber on a fresh loop
    async def main():
        q = events.subscribe()
        try:
            events.publish_round_closed({
                "round_number": 1, "ticks": [1, 2, 3], "events": 0,
                "events_by_type": {}, "next_round": 2,
                "next_opened_at": 1000.0, "eliminations": [],
            })
            first = await asyncio.wait_for(q.get(), timeout=5.0)
            second = await asyncio.wait_for(q.get(), timeout=5.0)
            seen.extend([first, second])
        finally:
            events.unsubscribe(q)

    _run(main())
    assert seen[0][0] == "round_closed"
    assert seen[0][1]["round_number"] == 1 and seen[0][1]["next_round"] == 2
    assert seen[1] == ("round_opened",
                       {"round": 2, "deadline_epoch": 1900.0})


def test_publish_round_closed_without_deadline(monkeypatch):
    monkeypatch.delenv("ECON_ROUND_DEADLINE_S", raising=False)
    seen = []

    async def main():
        q = events.subscribe()
        try:
            events.publish_round_closed({
                "round_number": 1, "ticks": [1], "events": 0,
                "events_by_type": {}, "next_round": 2,
                "next_opened_at": 1000.0, "eliminations": [],
            })
            seen.append(await asyncio.wait_for(q.get(), timeout=5.0))
            seen.append(await asyncio.wait_for(q.get(), timeout=5.0))
        finally:
            events.unsubscribe(q)

    _run(main())
    assert seen[1][1]["deadline_epoch"] is None
