"""The NIM rate limiter: one shared per-minute budget for the whole run.

Not a mock-heavy test — the limiter is 20 lines of monotonic-clock
arithmetic, so we test exactly that: bursts pass until the window is
full, the next call waits for the oldest entry to age out, and the
429-retry path consumes budget too (it is, after all, another request).
"""

import time

from experiments.agent.llm import _RateLimiter


def test_bursts_pass_then_wait():
    lim = _RateLimiter(3, window=0.4)
    t0 = time.monotonic()
    for _ in range(3):
        lim.wait()                          # a burst of 3 goes straight out
    assert time.monotonic() - t0 < 0.2
    lim.wait()                              # the 4th waits for the window
    assert time.monotonic() - t0 >= 0.35    # oldest entry aged out first


def test_window_slides():
    lim = _RateLimiter(1, window=0.3)
    lim.wait()
    time.sleep(0.35)
    t0 = time.monotonic()
    lim.wait()                              # window already drained — no wait
    assert time.monotonic() - t0 < 0.2
    assert len(lim._times) == 1             # only the fresh call recorded
