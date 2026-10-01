"""The background half of `arena serve`: checks new submissions, schedules and plays games
with a pool of judge workers, rates, and runs periodic tasks (the server bridge).

Workers call runner.play() in threads (each game is its own `unswbc run` process), sized by
[play].workers. Every finished game is written as games/<id>.json at once; a restart just
re-plays what was running.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from arena import rating, runner, scheduler, stats, store, submit
from arena.state import CHECKING, ENGINE, Arena, Game, now

log = logging.getLogger("arena")


class Daemon:
    def __init__(self, arena: Arena):
        self.arena = arena
        cfg = arena.cfg
        self.workers = int(cfg.get("play", "workers", 4))
        # workers = 0: a read-only arena (serves and imports, never plays or checks)
        self.pool = ThreadPoolExecutor(max(1, self.workers), thread_name_prefix="judge")
        self.checking: set[str] = set()
        self.tasks: list[tuple[float, Callable[[], None], str]] = []   # (period s, fn, name)
        self.last_run: dict[str, float] = {}
        self.stop = threading.Event()
        self.wake = threading.Event()
        arena.ratings = store.read_json(cfg.data_dir / "ratings.json", {}) or {}
        arena.daemon = self

    def install_maps(self):
        """Keeps the worldgen suites current: this week's rotating maps and the holdout."""
        from arena import maps
        self.every(3600, lambda: maps.ensure_all(self.arena.cfg), "maps")

    def every(self, seconds: float, fn: Callable[[], None], name: str):
        """Registers a periodic task (run in its own thread so it never blocks games)."""
        self.tasks.append((seconds, fn, name))

    # ------------------------------------------------------------------ loop
    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="dispatcher", daemon=True)
        t.start()
        return t

    def run(self):
        log.info("dispatcher: %d workers", self.workers)
        self.rate()
        while not self.stop.is_set():
            try:
                self._tick()
            except Exception:
                log.error("dispatcher error:\n%s", traceback.format_exc())
            self.wake.wait(2)
            self.wake.clear()

    def busy(self) -> int:
        return len(self.arena.running) + len(self.checking)

    def _tick(self):
        a = self.arena
        store.write_json(a.cfg.data_dir / "heartbeat.json",
                         {"at": now(), "running": len(a.running), "checking": sorted(self.checking)})
        for period, fn, name in self.tasks:
            if time.time() - self.last_run.get(name, 0) >= period:
                self.last_run[name] = time.time()
                threading.Thread(target=self._task, args=(fn, name), daemon=True).start()
        free = self.workers - self.busy()
        with a.lock:
            pending = [s for s in a.subs.values() if s.status == CHECKING and s.id not in self.checking]
        for s in sorted(pending, key=lambda s: s.created)[:max(0, free)]:
            self.checking.add(s.id)
            self.pool.submit(self._check, s)
            free -= 1
        if free <= 0:
            return
        scheduler.settle(a)
        for job in scheduler.next_jobs(a, free):
            with a.lock:
                a.running[job.key] = {"job": job, "started": now()}
            self.pool.submit(self._play, job)
        scheduler.settle(a)

    def _task(self, fn, name):
        try:
            fn()
        except Exception:
            log.error("task %s failed:\n%s", name, traceback.format_exc())

    # ------------------------------------------------------------------ work
    def _check(self, s):
        try:
            ok = submit.check(self.arena, s)
            log.info("check %s: %s", s.id, "ok" if ok else f"rejected ({s.reason})")
        except Exception as e:
            submit.reject(self.arena, s, f"internal error during checks: {e}", traceback.format_exc())
        finally:
            self.checking.discard(s.id)
            self.wake.set()

    def _play(self, job: scheduler.Job):
        a, cfg = self.arena, self.arena.cfg
        g = Game(id=0, lane=job.lane, suite=job.suite, map=Path(job.map).stem, seed=job.seed,
                 a=job.a, b=job.b, meta={"engine": ENGINE})
        try:
            r = runner.play(Path(job.map), cfg.bots / job.a, cfg.bots / job.b, job.seed,
                            cfg.work / "replays", work_root=cfg.work,
                            timeout=cfg.get("play", "game_timeout", 1800))
            g.winner, g.rounds, g.reason, g.seconds = r.winner, r.rounds, r.reason, round(r.seconds, 1)
            g.stats = stats.extract(r)
            replay = r.replay
        except runner.JudgeError as e:
            g.error = str(e)[:2000]
            replay = None
        except Exception as e:
            g.error = f"internal: {e}"
            replay = None
            log.error("game %s failed:\n%s", job.key, traceback.format_exc())
        a.add_game(g)
        if replay and replay.exists():
            cfg.replays.mkdir(parents=True, exist_ok=True)
            dest = cfg.replays / f"{g.id}.replay"
            shutil.move(str(replay), dest)
            g.replay = dest.name
            a.save_game(g)
        with a.lock:
            a.running.pop(job.key, None)
        log.info("game %d %s vs %s on %s: %s", g.id, g.a, g.b, g.map, g.error or g.winner or "draw")
        self.rate()
        scheduler.settle(a)
        self.wake.set()

    def rate(self):
        r = rating.fit(self.arena)
        self.arena.ratings = r
        store.write_json(self.arena.cfg.data_dir / "ratings.json", r)
