"""Fills in metrics for games played before a metric existed (or whose metric version changed):
newest first (real server games before local ones), at low priority so the judges and the website
never wait on it. With arena-metrics (native/metrics) it does thousands of games per batch; without
it, a few at a time in low-priority Python processes."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from arena import metrics, store

log = logging.getLogger("arena")
_POOL: ProcessPoolExecutor | None = None
_BUSY = __import__("threading").Lock()


def _nice():
    try:
        os.nice(15)
    except OSError:
        pass


def _one(replay: str) -> dict | None:
    try:
        return metrics.compute(Path(replay).read_bytes())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:300]}


# Games whose stored metrics are known current for these metric versions: each file is read
# once, not on every run (reading all ~60k files every 5 s kept the engine's process busy and its
# API slow). A metric version change starts over.
_current: set[int] = set()
_versions: tuple = ()


def _known(gid: int):
    _current.add(gid)


def todo(arena, limit: int) -> list:
    global _versions
    v = tuple(sorted(metrics.versions().items()))
    if v != _versions:
        _current.clear()
        _versions = v
    d = arena.cfg.data_dir / "metrics"
    with arena.lock:
        games = [g for g in arena.games if g.replay and g.id not in _current]
    games.sort(key=lambda g: (g.lane != "server", -g.id))
    out = []
    for g in games:
        p = d / f"{g.id}.json"
        if p.exists():
            stored = store.read_json(p, None)
            if stored and (stored.get("error") or not metrics.outdated(stored)):
                _known(g.id)
                continue
        out.append(g)
        if len(out) >= limit:
            break
    return out


def run(arena, batch: int | None = None):
    if not _BUSY.acquire(blocking=False):          # the previous batch is still going
        return
    try:
        from arena.metrics import native
        if native.usable():
            _run_native(arena, batch or 2000)
        else:
            _run(arena, batch or 6 * _procs(arena))
    finally:
        _BUSY.release()


def _run_native(arena, batch: int):
    """A batch in arena-metrics (C++): thousands of games a minute, on [metrics] backfill_procs
    threads at low priority."""
    from arena.metrics import native
    games = todo(arena, batch)
    d = arena.cfg.data_dir / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    pairs = [(arena.cfg.replays / g.replay, d / f"{g.id}.json") for g in games]
    res = native.compute_files(pairs, threads=_procs(arena), nice=15)
    for g, (replay, _) in zip(games, pairs):
        if str(replay) in res:                   # written: metrics or the error it hit
            _known(g.id)
    done = sum(1 for v in res.values() if v == "ok")
    if done:
        log.info("metrics: backfilled %d games (native)", done)


def _run(arena, batch: int):
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(_procs(arena),
                                    mp_context=mp.get_context("spawn"), initializer=_nice)
    games = todo(arena, batch)
    futs = {g.id: _POOL.submit(_one, str(arena.cfg.replays / g.replay)) for g in games}
    done = 0
    for gid, f in futs.items():
        r = f.result()
        if r is not None:
            store.write_json(arena.cfg.data_dir / "metrics" / f"{gid}.json", r)
            _known(gid)
            done += 1
    if done:
        log.info("metrics: backfilled %d games", done)


def _procs(arena) -> int:
    return int(arena.cfg.get("metrics", "backfill_procs", 4))


def install(daemon):
    daemon.every(5, lambda: run(daemon.arena), "metrics-backfill")
