"""Fills in metrics for games played before a metric existed (or whose metric version changed):
a few games at a time, newest first (real server games before local ones), in two low-priority
worker processes so the judges and the website never wait on it."""

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


def todo(arena, limit: int) -> list:
    d = arena.cfg.data_dir / "metrics"
    with arena.lock:
        games = [g for g in arena.games if g.replay]
    games.sort(key=lambda g: (g.lane != "server", -g.id))
    out = []
    for g in games:
        p = d / f"{g.id}.json"
        if p.exists():
            stored = store.read_json(p, None)
            if stored and (stored.get("error") or not metrics.outdated(stored)):
                continue
        out.append(g)
        if len(out) >= limit:
            break
    return out


def run(arena, batch: int = 8):
    if not _BUSY.acquire(blocking=False):          # the previous batch is still going
        return
    try:
        _run(arena, batch)
    finally:
        _BUSY.release()


def _run(arena, batch: int):
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(int(arena.cfg.get("metrics", "backfill_procs", 2)),
                                    mp_context=mp.get_context("spawn"), initializer=_nice)
    games = todo(arena, batch)
    futs = {g.id: _POOL.submit(_one, str(arena.cfg.replays / g.replay)) for g in games}
    done = 0
    for gid, f in futs.items():
        r = f.result()
        if r is not None:
            store.write_json(arena.cfg.data_dir / "metrics" / f"{gid}.json", r)
            done += 1
    if done:
        log.info("metrics: backfilled %d games", done)


def install(daemon):
    daemon.every(15, lambda: run(daemon.arena), "metrics-backfill")
