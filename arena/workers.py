"""Remote workers (docs/WORKERS.md): machines that join, lease games, return results, and leave.

The engine keeps the queue (scheduler.next_jobs); a worker leases games from it. A leased game
counts as running, so it's never handed out twice; a lease that isn't renewed by heartbeats
expires and the game is owed again. Holdout games are never leased: their maps stay on the
engine's machine.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field

from arena import scheduler
from arena.state import ENGINE, Arena, now

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30


class Refused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Worker:
    name: str
    host: str
    version: str
    slots: int
    token: str                      # the token's name, not the token
    first_seen: str = field(default_factory=now)
    last_seen: float = field(default_factory=time.time)
    done: list = field(default_factory=list)     # monotonic times of recorded results


def file_sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Registry:
    def __init__(self, arena: Arena, daemon):
        self.arena, self.daemon = arena, daemon
        self.lock = threading.Lock()
        self.workers: dict[str, Worker] = {}
        self.leases: dict[str, dict] = {}            # lease id -> {job, worker, expires}
        self.lease_seconds = int(arena.cfg.get("workers", "lease_seconds", LEASE_SECONDS))
        self._map_sha: dict[str, tuple] = {}

    # ------------------------------------------------------------------ auth and identity
    def token_name(self, bearer: str | None) -> str:
        tokens = self.arena.cfg.get("workers", "tokens", {}) or {}
        for name, tok in tokens.items():
            if bearer and secrets.compare_digest(str(tok), bearer):
                return name
        raise Refused("a valid worker token is required (Authorization: Bearer ...)", 401)

    def _touch(self, name: str, host: str, version: str, slots: int, token: str) -> Worker:
        if version != ENGINE:
            raise Refused(f"this arena judges with unswbc {ENGINE}; the worker has {version}", 409)
        with self.lock:
            w = self.workers.get(name)
            if w is None:
                w = self.workers[name] = Worker(name, host, version, slots, token)
            w.host, w.version, w.slots, w.token, w.last_seen = host, version, max(0, slots), token, time.time()
            return w

    # ------------------------------------------------------------------ the protocol
    def lease(self, name: str, host: str, version: str, slots: int, token: str, want: int) -> dict:
        self._touch(name, host, version, slots, token)
        a, out = self.arena, []
        if want > 0 and not self.daemon.paused_remote:
            jobs = [j for j in scheduler.next_jobs(a, want * 2) if j.suite != "holdout"][:want]
            with a.lock:
                for j in jobs:
                    if j.key in a.running:
                        continue
                    lid = secrets.token_hex(8)
                    expires = time.time() + self.lease_seconds
                    a.running[j.key] = {"job": j, "started": now(), "worker": name, "lease": lid}
                    with self.lock:
                        self.leases[lid] = {"job": j, "worker": name, "expires": expires}
                    out.append({"lease": lid, "game": self._describe(j)})
        return {"leases": out, "lease_seconds": self.lease_seconds,
                "heartbeat_seconds": HEARTBEAT_SECONDS, "engine": ENGINE}

    def _describe(self, j: scheduler.Job) -> dict:
        subs = self.arena.subs
        return {"a": {"id": j.a, "sha256": subs[j.a].sha256}, "b": {"id": j.b, "sha256": subs[j.b].sha256},
                "suite": j.suite, "map": {"name": self._map_name(j), "sha256": self.map_sha(j.map)},
                "seed": j.seed, "lane": j.lane,
                "timeout": self.arena.cfg.get("play", "game_timeout", 1800)}

    @staticmethod
    def _map_name(j: scheduler.Job) -> str:
        return j.map.rsplit("/", 1)[-1][:-4]

    def map_sha(self, path: str) -> str:
        from pathlib import Path
        p = Path(path)
        m = p.stat().st_mtime_ns
        hit = self._map_sha.get(path)
        if not hit or hit[0] != m:
            hit = self._map_sha[path] = (m, file_sha(p))
        return hit[1]

    def heartbeat(self, name: str, host: str, version: str, slots: int, token: str,
                  leases: list[str]) -> dict:
        self._touch(name, host, version, slots, token)
        renewed, cancelled = [], []
        with self.lock:
            for lid in leases:
                l = self.leases.get(lid)
                if l and l["worker"] == name:
                    l["expires"] = time.time() + self.lease_seconds
                    renewed.append(lid)
                else:
                    cancelled.append(lid)
        return {"renewed": renewed, "cancelled": cancelled}

    def result(self, name: str, token: str, lease: str, res: dict, replay=None) -> dict:
        with self.lock:
            l = self.leases.pop(lease, None)
            w = self.workers.get(name)
        if l is None:
            return {"recorded": False, "why": "unknown or expired lease"}
        if l["worker"] != name:
            return {"recorded": False, "why": "that lease belongs to another worker"}
        ok = self.daemon.record_result(l["job"], res, name, replay)
        if ok and w is not None:
            w.done.append(time.monotonic())
        return {"recorded": ok, **({} if ok else {"why": "already recorded (a duplicate)"})}

    # ------------------------------------------------------------------ upkeep
    def reap(self):
        """Drops expired leases (their games are owed again) and workers silent for a while."""
        t = time.time()
        with self.lock:
            dead = [lid for lid, l in self.leases.items() if l["expires"] < t]
            gone = [n for n, w in self.workers.items() if t - w.last_seen > 3 * HEARTBEAT_SECONDS + 30]
            expired = [self.leases.pop(lid) for lid in dead]
            for n in gone:
                self.workers.pop(n, None)
        if expired:
            with self.arena.lock:
                for l in expired:
                    r = self.arena.running.get(l["job"].key)
                    if r and r.get("worker") == l["worker"]:
                        self.arena.running.pop(l["job"].key, None)
            self.daemon.wake.set()

    def snapshot(self) -> list[dict]:
        cutoff = time.monotonic() - 3600
        with self.lock:
            running: dict[str, int] = {}
            for l in self.leases.values():
                running[l["worker"]] = running.get(l["worker"], 0) + 1
            out = []
            for w in self.workers.values():
                w.done = [x for x in w.done if x > cutoff]
                out.append({"name": w.name, "host": w.host, "version": w.version, "slots": w.slots,
                            "running": running.get(w.name, 0), "games_last_hour": len(w.done),
                            "token": w.token, "first_seen": w.first_seen,
                            "last_seen_s": round(time.time() - w.last_seen)})
        return sorted(out, key=lambda w: w["name"])


def install(arena: Arena, daemon) -> Registry | None:
    """Enables remote workers when [workers] tokens are configured."""
    daemon.paused_remote = False
    if not arena.cfg.get("workers", "tokens"):
        return None
    reg = Registry(arena, daemon)
    daemon.registry = reg
    daemon.every(10, reg.reap, "workers-reap")
    arena.workers = reg
    return reg
