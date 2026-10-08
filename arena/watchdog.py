"""Finds what makes the API slow: logs requests over SLOW_REQUEST_S and jobs over SLOW_JOB_S,
and when a request has waited STUCK_S, every thread's stack (once a request, at most every
DUMP_EVERY_S), which shows what was holding things up at that moment."""

from __future__ import annotations

import contextlib
import itertools
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("arena")

SLOW_REQUEST_S = 2.0
SLOW_JOB_S = 1.0
STUCK_S = 10.0
DUMP_EVERY_S = 30.0

_inflight: dict[int, tuple[str, float]] = {}       # request number -> (method and path, start)
_ids = itertools.count()
_lock = threading.Lock()
_started = False


@contextlib.contextmanager
def timed(what: str, slow: float = SLOW_JOB_S):
    t = time.monotonic()
    try:
        yield
    finally:
        took = time.monotonic() - t
        if took >= slow:
            log.warning("slow: %s took %.1f s", what, took)


def begin(what: str) -> int:
    _start()
    n = next(_ids)
    with _lock:
        _inflight[n] = (what, time.monotonic())
    return n


def end(n: int):
    with _lock:
        what, t = _inflight.pop(n, ("?", time.monotonic()))
    took = time.monotonic() - t
    if took >= SLOW_REQUEST_S:
        log.warning("slow request: %s took %.1f s", what, took)


def _start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_watch, name="watchdog", daemon=True).start()


def _watch():
    dumped: set[int] = set()
    last = 0.0
    while True:
        time.sleep(2)
        now = time.monotonic()
        with _lock:
            stuck = [(n, w, now - t) for n, (w, t) in _inflight.items() if now - t >= STUCK_S and n not in dumped]
            dumped &= set(_inflight)
        if not stuck or now - last < DUMP_EVERY_S:
            continue
        last = now
        dumped |= {n for n, _, _ in stuck}
        names = {t.ident: t.name for t in threading.enumerate()}
        out = [f"stuck: {w} waiting {age:.0f} s" for _, w, age in stuck]
        for ident, frame in sys._current_frames().items():
            if names.get(ident) == "watchdog":
                continue
            out.append(f"--- thread {names.get(ident, ident)}")
            out.append("".join(traceback.format_stack(frame)[-12:]).rstrip())
        log.warning("watchdog: requests stuck; all threads:\n%s", "\n".join(out))
