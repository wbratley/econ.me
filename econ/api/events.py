"""Server-push events — the always-on host's notification channel (M2a).

The round clock already resolves rounds in-request (consent) or under
the operator's hand; what was missing for always-connected seats is a
way to *hear* about it without polling. This module is the in-process
fan-out behind ``GET /rounds/events`` (SSE): a tiny pub/sub with one
design decision — publishers are ordinary sync request handlers running
in FastAPI's threadpool, subscribers are asyncio queues on the event
loop, so ``publish()`` crosses threads with ``call_soon_threadsafe``
and is a no-op when nobody is listening (the common case: no dashboard,
no connected seats, zero cost).

Deliberately NOT: MCP ``resources/subscribe`` (the SSE endpoint is the
five-pound hammer that works through any proxy and any language's HTTP
stack), and cross-process pub/sub (single-writer SQLite implies a single
uvicorn worker today; Redis can come with Postgres if the internet game
ever needs it).

Events are public read-only facts (game.md §9.1 puts readiness in the
same class as prices and standings): round numbers, tick counts, who
has consented. No seat secrets ride this channel.
"""
from __future__ import annotations

import asyncio
import threading

# The subscriber registry: (loop, queue) pairs. `subscribe()` runs on the
# loop thread (inside the SSE endpoint); `publish()` runs anywhere.
_lock = threading.Lock()
_subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []


def subscribe() -> asyncio.Queue:
    """Register the calling task's interest; returns the delivery queue.

    Call from the event loop thread (the SSE endpoint does). The queue
    is unbounded: round events are rare (a handful per round), so a
    slow consumer costs a few KB, never a dropped wake-up.
    """
    q: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subscribers.append((asyncio.get_running_loop(), q))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Idempotent deregister (the SSE endpoint's finally block)."""
    with _lock:
        for entry in list(_subscribers):
            if entry[1] is q:
                _subscribers.remove(entry)


def publish(event: str, data: dict) -> None:
    """Fan one event out to every connected stream. Thread-safe; a no-op
    with no subscribers. A dead loop (closed between subscribe and now)
    is dropped, not raised over — the stream it belonged to is gone."""
    with _lock:
        entries = list(_subscribers)
    for loop, q in entries:
        try:
            loop.call_soon_threadsafe(q.put_nowait, (event, data))
        except RuntimeError:
            with _lock:
                if (loop, q) in _subscribers:
                    _subscribers.remove((loop, q))


def publish_round_closed(summary: dict) -> None:
    """The uniform post-commit broadcast for a resolved round, whoever
    resolved it (final consent, operator advance, deadline backstop):
    a ``round_closed`` with the summary's public shape, then the
    ``round_opened`` every waiting seat actually sleeps on — with the
    deadline epoch when the deadline is armed, so a seat can budget its
    wall-clock without a second fetch."""
    from econ.api.rounds import round_deadline_s

    publish("round_closed", {
        "round_number": summary["round_number"],
        "ticks": summary["ticks"],
        "events": summary["events"],
        "events_by_type": summary["events_by_type"],
        "next_round": summary["next_round"],
        "eliminations": summary["eliminations"],
    })
    deadline = round_deadline_s()
    opened_at = summary.get("next_opened_at")
    publish("round_opened", {
        "round": summary["next_round"],
        "deadline_epoch": (opened_at + deadline)
        if deadline > 0 and opened_at is not None else None,
    })
