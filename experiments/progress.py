"""A progress bar with an ETA, for runs long enough that you need to know
whether to wait.

Runs here are minutes, not seconds, and are routinely launched under nohup
into a log file -- so this renders in place on a terminal and as periodic
one-line updates everywhere else, rather than either spraying thousands of
lines into a log or leaving a redirected run looking hung.

    with Progress(ticks, "tax_none") as bar:
        for _ in range(ticks):
            run_tick(session)
            bar.advance()
"""

import sys
import time


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class Progress:
    """Rate and ETA over the whole run so far. Deliberately not a windowed
    average: these runs get slower as they go (population and order-book
    growth), and an ETA that quietly assumes the current rate holds is worse
    than one that is visibly conservative."""

    WIDTH = 28

    def __init__(
        self,
        total: int,
        label: str = "",
        stream=None,
        min_interval: float = 0.5,
        enabled: bool = True,
    ) -> None:
        self.total = max(1, total)
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.done = 0
        self.started = time.perf_counter()
        self._last_render = 0.0
        self._interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        # A redirected run gets occasional lines instead of 60 a second.
        self.min_interval = min_interval if self._interactive else max(min_interval, 10.0)

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def advance(self, n: int = 1) -> None:
        self.done += n
        now = time.perf_counter()
        if now - self._last_render < self.min_interval and self.done < self.total:
            return
        self._last_render = now
        self._render(final=False)

    def close(self) -> None:
        if not self.enabled:
            return
        self._render(final=True)
        self.stream.write("\n")
        self.stream.flush()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def rate(self) -> float:
        elapsed = self.elapsed
        return self.done / elapsed if elapsed > 0 else 0.0

    def _render(self, final: bool) -> None:
        if not self.enabled:
            return
        fraction = min(1.0, self.done / self.total)
        rate = self.rate
        eta = (self.total - self.done) / rate if rate > 0 and not final else 0.0

        tail = (
            f"{self.done}/{self.total}  {rate:.1f}/s  "
            + (f"done in {_duration(self.elapsed)}" if final else f"eta {_duration(eta)}")
        )
        prefix = f"{self.label} " if self.label else ""

        if self._interactive:
            filled = int(self.WIDTH * fraction)
            bar = "#" * filled + "-" * (self.WIDTH - filled)
            self.stream.write(f"\r{prefix}[{bar}] {fraction:5.1%} {tail}\033[K")
        else:
            self.stream.write(f"{prefix}{fraction:5.1%} {tail}\n")
        self.stream.flush()
