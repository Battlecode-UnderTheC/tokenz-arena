"""`arena worker`: a remote judge (docs/WORKERS.md). Leases games from an arena, runs them with
the official judge on this machine, and returns the results with their replays.

    arena worker --arena https://tokenz-arena.<tailnet>.ts.net --token ... --slots 16

Bots and maps are cached by content hash under --cache, so each is downloaded once per
machine; the bot folder keeps its submission id as its name, so the judge's wasm build cache
compiles each C/C++ bot once per machine too.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import multiprocessing as mp
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
import concurrent.futures as cf
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from arena import judge
from arena.state import ENGINE

log = logging.getLogger("arena.worker")
TAIL: collections.deque = collections.deque(maxlen=100)    # the last log lines, sent on exit


class _Tail(logging.Handler):
    def emit(self, record):
        try:
            TAIL.append(self.format(record)[:500])
        except Exception:
            pass
_BUILD: list = []


def build_id() -> str:
    """This worker's code: a hash of the files that lease, play and return games. The engine
    computes the same from its own copy, so a rented machine on an older image is drained and
    replaced (arena/workers.py Registry.draining, arena/scaler.py)."""
    if not _BUILD:
        here = Path(__file__).parent
        h = hashlib.sha256()
        for f in ("worker.py", "judge.py", "runner.py", "strictjudge.py"):
            p = here / f
            h.update(f.encode() + b"\0" + (p.read_bytes() if p.exists() else b""))
        _BUILD.append(h.hexdigest()[:16])
    return _BUILD[0]
DEFAULT_ARENA = "https://tokenz-arena.lonk-kelvin.ts.net"


class Worker:
    def __init__(self, arena: str, token: str, name: str, slots: int, cache: Path,
                 idle_exit: float = 0, prefetch: int | None = None):
        self.arena, self.token, self.name, self.slots = arena.rstrip("/"), token, name, slots
        # games leased beyond the slots: they fetch their bots and map, then wait in the
        # process pool's queue, so a slot starts its next game the moment one ends (no lease
        # round trip, and the result's upload overlaps the next game)
        self.prefetch = max(2, slots // 8) if prefetch is None else max(0, prefetch)
        self.cache = cache
        self.active: dict[str, dict] = {}          # lease -> game
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.heartbeat_seconds = 30
        self.procs = self._new_pool()
        self.pool_lock = threading.Lock()
        self.prep = cf.ThreadPoolExecutor(16, thread_name_prefix="prep")    # fetch bots, map; start
        self.send = cf.ThreadPoolExecutor(16, thread_name_prefix="send")    # return results
        self.fetch_lock = threading.Lock()
        self.wasm_locks: dict = collections.defaultdict(threading.Lock)
        self.idle_exit = idle_exit                 # exit after this long with nothing to do (0: never)
        self.last_work = time.monotonic()
        self.last_contact = time.monotonic()       # the arena last answered
        self.orphan_after = 0.0                    # exit after this long without the arena (0: never)
        self.exit_reason = ""                      # why run() ended, told to the arena on exit

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max(1, self.slots), mp_context=mp.get_context("spawn"))

    def _rebuild_pool(self, broken: ProcessPoolExecutor):
        """One game process died abruptly (killed for memory, say): the pool refuses every game
        after it, so a new one replaces it, once, by whichever game noticed first."""
        with self.pool_lock:
            if self.procs is broken:
                log.error("the process pool broke: starting a new one")
                self.procs = self._new_pool()
                broken.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ http
    def _req(self, method: str, path: str, body: bytes | None = None, ctype: str | None = None,
             timeout: float = 120) -> bytes:
        req = urllib.request.Request(self.arena + path, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", f"arena-worker/{ENGINE}")
        if ctype:
            req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _json(self, path: str, payload: dict) -> dict:
        return json.loads(self._req("POST", path, json.dumps(payload).encode(), "application/json"))

    def _hello(self) -> dict:
        cur, mx = self.pids()
        return {"worker": self.name, "version": ENGINE, "slots": self.slots, "build": build_id(),
                "pids": cur, "pids_max": mx}

    # ------------------------------------------------------------------ cache
    def _fetch(self, path: str, dest: Path, sha: str) -> Path:
        with self.fetch_lock:
            if dest.exists():
                return dest
            data = self._req("GET", path, timeout=300)
            got = hashlib.sha256(data).hexdigest()
            if got != sha:
                raise RuntimeError(f"{path}: sha256 {got[:12]} doesn't match the lease's {sha[:12]}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex}")
            tmp.write_bytes(data)
            tmp.rename(dest)
            return dest

    def bot(self, b: dict) -> Path:
        from arena.submit import _unpack
        root = self.cache / "bots" / b["sha256"][:24] / b["id"]
        if not (root / "bot.toml").exists():
            z = self._fetch(f"/api/v1/workers/bot/{b['id']}", self.cache / "zips" / f"{b['sha256']}.zip", b["sha256"])
            with self.fetch_lock:
                if not (root / "bot.toml").exists():
                    _unpack(z.read_bytes(), root)
        self.wasm(b["id"], root)
        return root

    def wasm(self, sid: str, root: Path):
        """Puts the engine's build of this bot into the judge's cache, so the first game here
        doesn't compile it (~75 s a bot). Anything off (a Python bot, an unswbc with other
        flags, a network error) just leaves the judge to build it, once, under its lock."""
        from arena import runner
        try:
            local = runner.wasm_path(root)
        except Exception:
            return
        if local is None or local.exists():
            return
        with self.wasm_locks[sid]:
            if local.exists():
                return
            try:
                req = urllib.request.Request(self.arena + f"/api/v1/workers/wasm/{sid}")
                req.add_header("Authorization", f"Bearer {self.token}")
                with urllib.request.urlopen(req, timeout=600) as r:
                    name, data = r.headers.get("X-Wasm-Name"), r.read()
            except Exception as e:
                log.warning("no prebuilt wasm for %s (%s): the judge will build it here", sid, e)
                return
            if name != local.name:
                log.warning("the engine's build of %s is %s, ours would be %s: building here", sid, name, local.name)
                return
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(local.name + f".{uuid.uuid4().hex}")
            tmp.write_bytes(data)
            tmp.rename(local)
            log.info("got %s's wasm from the engine (%d KB)", sid, len(data) // 1024)

    def map(self, suite: str, m: dict) -> Path:
        return self._fetch(f"/api/v1/workers/map/{suite}/{m['name']}",
                           self.cache / "maps" / m["sha256"][:24] / f"{m['name']}.map", m["sha256"])

    # ------------------------------------------------------------------ games
    def play(self, lease: str, game: dict):
        """Starts one leased game without a thread of its own: a small pool fetches its bots and
        map and hands it to the process pool, whose callback sends the result from another small
        pool. (A thread per game hit the container's process/thread limit at a few hundred slots:
        "can't start new thread", and the machine destroyed itself.)"""
        self.prep.submit(self._prepare, lease, game)

    def _prepare(self, lease: str, game: dict):
        pool = self.procs
        try:
            a, b = self.bot(game["a"]), self.bot(game["b"])
            m = self.map(game["suite"], game["map"])
            work = self.cache / "work"
            work.mkdir(parents=True, exist_ok=True)
            fut = pool.submit(judge.run_game, str(m), str(a), str(b), int(game["seed"]),
                              str(work / "replays"), str(work), float(game.get("timeout", 1800)),
                              game.get("judge", "official"), bool(game.get("check")))
        except Exception as e:
            self.send.submit(self._finish, lease, None, e, pool)
            return
        fut.add_done_callback(lambda f: self.send.submit(self._finish, lease, f, None, pool))

    def _finish(self, lease: str, fut, err: Exception | None, pool):
        replay = None
        try:
            try:
                if err is not None:
                    raise err
                res = fut.result()
            except (BrokenProcessPool, RuntimeError) as e:
                if isinstance(e, BrokenProcessPool) or "after shutdown" in str(e):
                    self._rebuild_pool(pool)
                raise                       # reported as the worker's failure: the engine replays it
            replay = Path(res.pop("replay")) if res.get("replay") else None
        except Exception as e:
            log.error("lease %s failed: %s: %s", lease, type(e).__name__, str(e)[:300])
            res = {"error": f"worker {self.name}: {type(e).__name__}: {e}"[:2000]}
        try:
            self._send_result(lease, res, replay)
        except Exception:
            log.exception("couldn't return lease %s (it will expire and be played again)", lease)
        finally:
            if replay and replay.exists():
                replay.unlink()
            with self.lock:
                self.active.pop(lease, None)

    @staticmethod
    def pids() -> tuple[int | None, int | None]:
        """(tasks now, the most allowed) for this container's cgroup: processes and threads both
        count. (None, None) where there's no limit to read."""
        for base in ("/sys/fs/cgroup", "/sys/fs/cgroup/pids"):
            try:
                cur = int(Path(base, "pids.current").read_text())
                mx = Path(base, "pids.max").read_text().strip()
                return cur, (None if mx == "max" else int(mx))
            except (OSError, ValueError):
                continue
        return None, None

    def goodbye(self, reason: str):
        """Tells the arena why this worker is stopping, with its last log lines, before a rented
        machine destroys itself (and its logs with it). Best effort."""
        try:
            self._req("POST", "/api/v1/workers/bye",
                      json.dumps({**self._hello(), "reason": reason[:4000], "log": list(TAIL)}).encode(),
                      "application/json", timeout=20)
        except Exception as e:
            log.warning("couldn't say goodbye: %s", e)

    def _send_result(self, lease: str, res: dict, replay: Path | None):
        edge = uuid.uuid4().hex
        meta = json.dumps({**res, "lease": lease, "worker": self.name}).encode()
        body = (f'--{edge}\r\nContent-Disposition: form-data; name="meta"\r\n\r\n'.encode() + meta + b"\r\n")
        if replay and replay.exists():
            body += (f'--{edge}\r\nContent-Disposition: form-data; name="replay"; filename="game.replay"\r\n'
                     "Content-Type: application/octet-stream\r\n\r\n").encode() + replay.read_bytes() + b"\r\n"
        body += f"--{edge}--\r\n".encode()
        out = json.loads(self._req("POST", "/api/v1/workers/result", body,
                                   f"multipart/form-data; boundary={edge}", timeout=300))
        log.info("lease %s: %s", lease, "recorded" if out.get("recorded") else out.get("why"))

    # ------------------------------------------------------------------ loops
    def _heartbeats(self):
        while not self.stopping.wait(self.heartbeat_seconds):
            with self.lock:
                leases = list(self.active)
            try:
                self._json("/api/v1/workers/heartbeat", {**self._hello(), "leases": leases})
                self.last_contact = time.monotonic()
            except Exception as e:
                log.warning("heartbeat failed: %s", e)

    def run(self):
        threading.Thread(target=self._heartbeats, daemon=True).start()
        log.info("worker %s: %d slots (+%d queued), judge unswbc %s, arena %s", self.name, self.slots,
                 self.prefetch, ENGINE, self.arena)
        idle = 0
        while not self.stopping.is_set():
            with self.lock:
                free = self.slots + self.prefetch - len(self.active)
            cur, mx = self.pids()
            if mx and cur is not None and cur > 0.8 * mx:
                if free > 0:
                    log.warning("%d of %d processes/threads in use: taking no new games for now", cur, mx)
                free = 0                    # near the container's limit: wait for games to finish
            if free <= 0:
                time.sleep(1)
                continue
            try:
                out = self._json("/api/v1/workers/lease", {**self._hello(), "want": free})
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                log.error("lease refused: HTTP %s %s", e.code, detail)
                if e.code in (401, 409):
                    log.error("worker refused (token revoked or wrong judge): stopping")
                    self.exit_reason = f"refused: HTTP {e.code} {detail[:200]}"
                    self.stopping.set()
                    break
                time.sleep(15)
                continue
            except Exception as e:
                log.warning("can't reach the arena: %s", e)
                if self.orphan_after and time.monotonic() - self.last_contact > self.orphan_after:
                    log.error("no arena for %ds: stopping", self.orphan_after)
                    self.exit_reason = f"no arena for {self.orphan_after:.0f} s"
                    self.stopping.set()
                    break
                time.sleep(15)
                continue
            self.last_contact = time.monotonic()
            self.heartbeat_seconds = out.get("heartbeat_seconds", 30)
            leases = out.get("leases") or []
            for l in leases:
                with self.lock:
                    self.active[l["lease"]] = l["game"]
                try:
                    self.play(l["lease"], l["game"])
                except RuntimeError as e:     # can't even queue it: its lease expires, it's replayed
                    log.error("couldn't start lease %s: %s", l["lease"], e)
                    with self.lock:
                        self.active.pop(l["lease"], None)
            idle = 0 if leases else min(idle + 1, 6)
            if leases:
                self.last_work = time.monotonic()
            elif self.idle_exit and not self.active and time.monotonic() - self.last_work > self.idle_exit:
                log.info("idle for %ds: exiting", self.idle_exit)
                self.exit_reason = f"idle for {self.idle_exit:.0f} s"
                self.stopping.set()
                break
            time.sleep(1 if leases else 5 * idle)
        log.info("stopping: waiting for %d running games", len(self.active))
        deadline = time.time() + 600
        while self.active and time.time() < deadline:
            time.sleep(2)


def main(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    tail = _Tail()
    tail.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(tail)
    env = os.environ.get
    token = args.token or env("ARENA_WORKER_TOKEN")
    arena = args.arena or env("ARENA_URL") or DEFAULT_ARENA
    if not token:
        raise SystemExit("needs a worker token: --token or ARENA_WORKER_TOKEN")
    slots = args.slots or int(env("ARENA_SLOTS") or 0) or max(1, int((os.cpu_count() or 2) / 1.5))
    idle = args.idle_exit if args.idle_exit is not None else float(env("ARENA_IDLE_EXIT") or 0)
    w = Worker(arena, token, args.name or env("WORKER_NAME") or socket.gethostname(), slots,
               Path(args.cache or env("ARENA_CACHE") or "~/.cache/tokenz-arena-worker").expanduser(), idle,
               int(env("ARENA_PREFETCH")) if env("ARENA_PREFETCH") else None)
    w.orphan_after = float(env("ARENA_ORPHAN_MINUTES") or 0) * 60
    try:                 # hundreds of slots need more files and processes than the defaults allow
        import resource
        for lim in (resource.RLIMIT_NOFILE, resource.RLIMIT_NPROC):
            soft, hard = resource.getrlimit(lim)
            if soft != hard:
                resource.setrlimit(lim, (hard, hard))
        log.info("limits: files %s, processes %s, cgroup tasks %s", resource.getrlimit(resource.RLIMIT_NOFILE)[0],
                 resource.getrlimit(resource.RLIMIT_NPROC)[0], Worker.pids())
    except (ImportError, ValueError, OSError) as e:
        log.warning("couldn't raise the file/process limits: %s", e)
    def stop(signum, _):
        w.exit_reason = w.exit_reason or f"signal {signum}"
        w.stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        w.run()
    except BaseException as e:
        import traceback
        w.exit_reason = f"crashed: {type(e).__name__}: {e}\n" + traceback.format_exc()[-3000:]
        raise
    finally:
        w.goodbye(w.exit_reason or "run() returned")
        if env("ARENA_SELF_DESTROY") == "1":
            self_destroy(env("CONTAINER_ID"), env("CONTAINER_API_KEY"))
    return 0


def self_destroy(instance: str | None, key: str | None):
    """On a rented vast.ai machine (docs/SCALER.md): when the worker stops for any reason (idle,
    no arena, token revoked), destroy this instance so it stops billing even if the scaler is
    down. Vast gives every container its own id and an API key for itself."""
    if not instance or not key:
        log.error("self-destroy asked for, but CONTAINER_ID / CONTAINER_API_KEY aren't set")
        return
    req = urllib.request.Request(f"https://console.vast.ai/api/v0/instances/{int(instance)}/", method="DELETE",
                                 data=b"{}", headers={"Authorization": f"Bearer {key}",
                                                     "Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                log.info("destroyed this instance (%s): %s", instance, r.read()[:200].decode(errors="replace"))
                return
        except Exception as e:
            log.error("self-destroy attempt %d failed: %s", attempt + 1, e)
            time.sleep(10)
