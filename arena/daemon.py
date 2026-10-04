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
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from arena import judge, judge_checks, queue, rating, scheduler, store, submit
from arena.state import CHECKING, ENGINE, INFRA_TRIES, Arena, Game, infra_error, now

log = logging.getLogger("arena")
RATE_SECONDS = 15


class Daemon:
    def __init__(self, arena: Arena):
        self.arena = arena
        cfg = arena.cfg
        self.workers = int(cfg.get("play", "workers", 4))
        # workers = 0: a read-only arena (serves and imports, never plays or checks)
        self.pool = ThreadPoolExecutor(max(1, self.workers), thread_name_prefix="judge")
        self.check_slots = max(1, int(cfg.get("play", "check_slots", 2)))
        self.checks = ThreadPoolExecutor(self.check_slots, thread_name_prefix="check")
        # Each game (the judge run and parsing its log) happens in a worker process; the
        # threads above only wait on them, so the engine's own process stays responsive.
        self.procs = ProcessPoolExecutor(max(1, self.workers), mp_context=mp.get_context("spawn"))
        self.checking: set[str] = set()
        self.tasks: list[tuple[float, Callable[[], None], str]] = []   # (period s, fn, name)
        self.last_run: dict[str, float] = {}
        self.stop = threading.Event()
        self.wake = threading.Event()
        arena.ratings = store.read_json(cfg.data_dir / "ratings.json", {}) or {}
        arena.daemon = self
        self.registry = None                 # arena.workers.Registry once remote workers are enabled
        self.paused_remote = False
        self._snapshot_at = 0.0
        # Ratings are refit at most every RATE_SECONDS, not after each game: a refit reads every
        # game, and doing it per result (about one a second with a rented machine) kept the
        # engine's process busy enough that its API took 10-20 s.
        self._rate_due = False
        self._infra_tries: dict = {}      # game key -> machine failures so far (arena/state.py infra_error)
        self.every(RATE_SECONDS, self._rate_if_due, "ratings")
        from arena import duels
        self.every(300, lambda: duels.get(arena).expire(), "duels-expire")

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
        """Slots of the engine's own pool in use (games leased to remote workers don't count)."""
        # a copy: result threads add and remove running games while this counts
        return sum(1 for r in list(self.arena.running.values()) if r.get("worker", "local") == "local")

    def _tick(self):
        a = self.arena
        with a.lock:
            running = [{"priority": r["job"].priority, "a": r["job"].a, "b": r["job"].b,
                        "suite": r["job"].suite, "map": r["job"].map, "seed": r["job"].seed,
                        "owner": r["job"].owner, "for_sub": r["job"].for_sub, "started": r["started"],
                        "worker": r.get("worker", "local")}
                       for r in a.running.values()]
        remote = self.registry.snapshot() if self.registry else []
        store.write_json(a.cfg.data_dir / "heartbeat.json",
                         {"at": now(), "workers": self.workers + sum(w["slots"] for w in remote),
                          "local_workers": self.workers, "remote": remote, "running": running,
                          "checking": sorted(self.checking)})
        for period, fn, name in self.tasks:
            if time.time() - self.last_run.get(name, 0) >= period:
                self.last_run[name] = time.time()
                threading.Thread(target=self._task, args=(fn, name), daemon=True).start()
        # Checks have their own slots, so new submissions never wait behind games (and still
        # run when every game goes to remote workers).
        check_free = self.check_slots - len(self.checking)
        with a.lock:
            pending = [s for s in a.subs.values() if s.status == CHECKING and s.id not in self.checking]
        from arena import duels
        book = duels.get(a)
        with book.lock:
            pending += [e for e in book.ephemeral.values() if e.status == CHECKING and e.id not in self.checking]
        for s in sorted(pending, key=lambda s: s.created)[:max(0, check_free)]:
            self.checking.add(s.id)
            self.checks.submit(self._check_ephemeral if duels.is_ephemeral(s.id) else self._check, s)
        q = queue.get(a)
        q.sync()
        if time.monotonic() - self._snapshot_at >= queue.SNAPSHOT_SECONDS:
            self._snapshot_at = time.monotonic()
            q.snapshot()
        free = self.workers - self.busy()
        if free <= 0:
            return
        for job in q.take(free, "local"):
            self.pool.submit(self._play, job)

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

    def _check_ephemeral(self, e):
        from arena import duels
        try:
            ok = duels.get(self.arena).check_ephemeral(e)
            log.info("check %s: %s", e.id, "ok" if ok else f"rejected ({e.reason})")
        finally:
            self.checking.discard(e.id)
            self.wake.set()

    def _play(self, job: scheduler.Job):
        cfg = self.arena.cfg
        try:
            res = self.procs.submit(judge.run_game, job.map, str(cfg.bots / job.a),
                                    str(cfg.bots / job.b), job.seed, str(cfg.work / "replays"),
                                    str(cfg.work), cfg.get("play", "game_timeout", 1800),
                                    cfg.get("play", "judge", "official"),
                                    judge.spot_check(cfg, job.key)).result()
        except Exception as e:
            res = {"error": f"internal: {e}"}
            log.error("game %s failed:\n%s", job.key, traceback.format_exc())
        self.record_result(job, res, "local")

    def record_result(self, job: scheduler.Job, res: dict, worker: str,
                      replay: Path | None = None) -> bool:
        """Records one finished game, from the engine's own pool or a remote worker. Returns
        False (and records nothing) if that game was already recorded meanwhile, e.g. when an
        expired lease's game was played again elsewhere and both results arrive."""
        a, cfg = self.arena, self.arena.cfg
        q = queue.get(a)
        if job.duel:                     # a duel game: stored with its duel, never an arena Game
            if replay is None and res.get("replay"):
                replay = Path(res["replay"])
            ok = q.record_duel(job, res, worker, replay)
            log.info("duel game %s %s vs %s on %s (%s): %s", job.duel, job.a, job.b, Path(job.map).stem,
                     worker, res.get("error") or res.get("winner") or "draw")
            self.wake.set()
            return ok
        if not job.duel and infra_error(res.get("error")):
            seen = self._infra_tries.setdefault(job.key, set())
            seen.add(worker)
            n = len(seen)
            if n < INFRA_TRIES:                # the machine failed, not the bots: play it again
                log.info("game %s vs %s on %s failed on %s (%s): owed again (%d of %d machines)", job.a, job.b,
                         Path(job.map).stem, worker, str(res["error"])[:120], n, INFRA_TRIES)
                queue.get(self.arena).release(job, worker)
                self.wake.set()
                return False
        g = Game(id=0, lane=job.lane, suite=job.suite, map=Path(job.map).stem, seed=job.seed,
                 a=job.a, b=job.b, meta={"engine": ENGINE, "worker": worker})
        if infra_error(res.get("error")):      # failed on INFRA_TRIES machines: recorded as it is
            g.meta["final_error"] = True
            g.meta["final_workers"] = sorted(self._infra_tries.get(job.key, ()))
        try:
            from arena.maps import file_sha
            g.meta["map_sha"] = file_sha(Path(job.map))       # the map content it was played on
        except OSError:
            pass
        if res.get("error"):
            g.error = res["error"]
        else:
            g.winner, g.rounds, g.reason = res["winner"], res["rounds"], res["reason"]
            g.seconds, g.stats = res.get("seconds"), res.get("stats") or {}
            g.meta["judge"] = res.get("judge", "official")
            if res.get("judge_check"):
                g.meta["judge_check"] = res["judge_check"]
                judge_checks.note(cfg, res["judge_check"], job.key, worker)
            if replay is None and res.get("replay"):
                replay = Path(res["replay"])
        with a.lock:                     # check, store and record in one step: never twice
            if q.is_played(job.key):
                r = a.running.get(job.key)
                if r and r.get("worker", "local") == worker:
                    a.running.pop(job.key, None)
                log.info("game %s from %s: already recorded, dropped", job.key, worker)
                return False
            a.add_game(g)
            q.record(g, job)
        if res.get("metrics"):
            store.write_json(cfg.data_dir / "metrics" / f"{g.id}.json", res["metrics"])
        if replay and replay.exists():
            cfg.replays.mkdir(parents=True, exist_ok=True)
            dest = cfg.replays / f"{g.id}.replay"
            from arena import retention
            if retention.is_gzip(replay):
                shutil.move(str(replay), dest)
            else:
                retention.compress_file(replay, dest)        # stored gzip-compressed
            g.replay = dest.name
            a.save_game(g)
        log.info("game %d %s vs %s on %s (%s): %s", g.id, g.a, g.b, g.map, worker,
                 g.error or g.winner or "draw")
        self._rate_due = True
        self.wake.set()
        return True

    def _rate_if_due(self):
        if self._rate_due:
            self._rate_due = False
            self.rate()

    def rate(self):
        r = rating.fit(self.arena)
        self.arena.ratings = r
        store.write_json(self.arena.cfg.data_dir / "ratings.json", r)
