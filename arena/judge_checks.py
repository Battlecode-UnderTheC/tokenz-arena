"""cppjudge spot checks (arena/judge.py spot_check): how many games cppjudge played that the
official judge, playing them again, agreed with. Kept in data/judge-checks.json; the website
reads the file. Any disagreement is listed: the game counted the official judge's result, and
cppjudge needs a look."""

from __future__ import annotations

import logging
import threading

from arena import store
from arena.state import now

log = logging.getLogger("arena")
_LOCK = threading.Lock()


def path(cfg):
    return cfg.data_dir / "judge-checks.json"


def read(cfg) -> dict:
    return store.read_json(path(cfg), None) or {"same": 0, "different": 0, "differences": []}


def note(cfg, outcome: str, key: str, worker: str) -> None:
    with _LOCK:
        d = read(cfg)
        d[outcome] = d.get(outcome, 0) + 1
        if outcome == "different":
            log.warning("cppjudge disagreed with the official judge on game %s (worker %s)", key, worker)
            d["differences"] = (d.get("differences") or [])[-49:] + [{"game": key, "worker": worker, "at": now()}]
        d["updated"] = now()
        store.write_json(path(cfg), d)
