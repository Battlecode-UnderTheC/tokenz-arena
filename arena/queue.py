"""The game queue (DESIGN.md section 3): the engine's live, in-memory picture of every game owed,
running and played, kept up to date by events instead of being recomputed from all games.

Which games are owed is unchanged (scheduler.owed_games, holdout and continuous pairing), and so
is the order they're played in: priority (drafts, placement, holdout, continuous), then the
owner with the fewest running games, then the oldest submission, then map and seed (ties by
submission id and game key, so the order never depends on what was picked before).

State, all under arena.lock:
    played    keys (a, b, suite, map, seed) of games that count now (state.current_game)
    jobs      {sid: {key: Job}} each submission's owed games; owners {key: {sid}} the reverse
    pending   {sid: deque[Job]} its owed games not yet played, in play order
    left      {sid: games owed and not yet played}
    met       {frozenset(a, b): rated games}, for continuous pairing
    sigs      {sid: signature}; a submission whose signature changes is recomputed
    stamp     the maps, seeds, judge rules and usable submissions everything depends on; a change
              rebuilds it all (rare: a new map, a map re-published, a new submission)

Events: take() starts games (local judges and worker leases), record() takes a finished game,
release() returns an abandoned one, sync() (every engine tick) catches submissions whose status,
parent or opponents changed. Each bumps `version`; view() is what /api/v1/queue shows, and
wait(version) lets the live page and long polls hear about the next change at once.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

from arena import store
from arena.state import (DONE, PLACING, PLAYING, QUEUED, RATED, Arena, current_game, is_rated, now,
                         rules)

log = logging.getLogger("arena")

OWING = (QUEUED, PLAYING, DONE, PLACING, RATED)      # statuses scheduler.owed_games answers for
AUDIT_SECONDS = 600
SNAPSHOT_SECONDS = 2.0


def _sig(s) -> tuple:
    return (s.status, s.lane, s.parent, tuple(s.opponents) if s.opponents is not None else None,
            tuple(s.quick_opponents) if s.quick_opponents is not None else None)


class _Taken:
    """`key in taken`: played or running (what the holdout and continuous pickers skip)."""

    def __init__(self, played: set, running: dict):
        self.played, self.running = played, running

    def __contains__(self, key) -> bool:
        return key in self.played or key in self.running


class GameQueue:
    def __init__(self, arena: Arena):
        self.arena = arena
        self.cond = threading.Condition()
        self.version = 0
        self._view: tuple[int, dict] | None = None
        self.played: set = set()
        self.jobs: dict[str, dict] = {}
        self.owners: dict[tuple, set] = collections.defaultdict(set)
        self.pending: dict[str, collections.deque] = {}
        self.left: dict[str, int] = {}
        self.met: collections.Counter = collections.Counter()
        self.sigs: dict[str, tuple] = {}
        self.stamp = None
        self.last_gid = 0                         # games up to this id are in `played`
        self.avg_s = 30.0                         # recent games' mean length, for the scaler
        self.last_audit = time.monotonic()
        self.rebuild()

    # ------------------------------------------------------------------ bookkeeping
    def _bump(self):
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def _stamp(self) -> tuple:
        from arena import maps as M
        from arena import scheduler as S
        a = self.arena
        suites = tuple((x, tuple(map(str, S.maps(a, x))))
                       for x in ("official", "tournament", "server", "custom", "rotating"))
        seeds = tuple(sorted((k, tuple(v)) for k, v in S.server_seeds(a).items()))
        live = tuple(sorted(M.live(a.cfg)[0].items()))
        usable = tuple(sorted(sid for sid in a.subs if S._usable(a, sid)))
        from arena.state import ENGINE
        return (suites, seeds, hash(live), usable, rules(ENGINE))

    def rebuild(self):
        """Everything from scratch: at start, when the maps, seeds or usable submissions change,
        and as a periodic audit of the incremental state."""
        a = self.arena
        t = time.monotonic()
        with a.lock:
            played = {(g.a, g.b, g.suite, g.map, g.seed) for g in a.games if current_game(g)}
            met = collections.Counter(frozenset((g.a, g.b)) for g in a.games if is_rated(g))
            old_left = dict(self.left)
            self.played, self.met = played, met
            self.last_gid = a.games[-1].id if a.games else 0
            recent = [g.seconds for g in a.games[-500:] if g.seconds and not g.error and g.lane != "server"]
            if recent:
                self.avg_s = sum(recent) / len(recent)
            self.jobs, self.owners, self.pending, self.left, self.sigs = {}, collections.defaultdict(set), {}, {}, {}
            for s in list(a.subs.values()):
                self._load_sub(s)
            self.stamp = self._stamp()
            for sid in list(self.jobs):
                self._settle(sid)
        if old_left and old_left != self.left:
            drift = {k: (old_left.get(k), self.left.get(k)) for k in set(old_left) | set(self.left)
                     if old_left.get(k) != self.left.get(k)}
            log.info("queue rebuilt in %.2fs; %d submissions changed (%s)", time.monotonic() - t,
                     len(drift), ", ".join(f"{k} {v[0]}->{v[1]}" for k, v in list(drift.items())[:5]))
        self.last_audit = time.monotonic()
        self._bump()

    def _drop(self, sid: str):
        for key in self.jobs.pop(sid, {}):
            self.owners[key].discard(sid)
            if not self.owners[key]:
                del self.owners[key]
        self.pending.pop(sid, None)
        self.left.pop(sid, None)

    def _load_sub(self, s):
        """(Re)computes one submission's owed games. Caller holds arena.lock."""
        from arena import scheduler as S
        sid = s.id
        self._drop(sid)
        owed = S.owed_games(self.arena, s) if s.status in OWING else []
        self.sigs[sid] = _sig(s)                  # after owed_games: it may freeze opponents
        if not owed:
            return
        jobs: dict = {}
        for j in owed:
            jobs.setdefault(j.key, j)
        self.jobs[sid] = jobs
        for key in jobs:
            self.owners[key].add(sid)
        todo = sorted((j for k, j in jobs.items() if k not in self.played), key=lambda j: (j.map, j.seed, j.key))
        self.pending[sid] = collections.deque(todo)
        self.left[sid] = len(todo)

    def _settle(self, sid: str, running_sids: set | None = None):
        """Status moves (caller holds arena.lock): queued -> playing when a game starts,
        draft -> done and placing -> rated when all are played, done -> playing when some
        stopped counting. Returns True if the status changed (its owed games then change)."""
        a = self.arena
        s = a.subs.get(sid)
        if s is None:
            return False
        if running_sids is None:
            running_sids = self._running_sids()
        left = self.left.get(sid, 0)
        new = s.status
        if s.status == DONE and s.lane == "draft":
            if left:
                new = PLAYING
        elif s.status in (QUEUED, PLAYING, PLACING):
            if sid in running_sids:
                if s.status == QUEUED:
                    new = PLAYING
            elif sid in self.jobs and not left:
                new = DONE if s.lane == "draft" else RATED
        if new == s.status:
            return False
        s.status = new
        a.save_sub(s)
        self._load_sub(s)
        return True

    def _running_sids(self) -> set:
        out = set()
        for key in self.arena.running:
            out |= self.owners.get(key, set())
        return out

    # ------------------------------------------------------------------ events
    def sync(self):
        """Catches what changed without an event: submissions whose status, parent or
        opponents changed (submit, check, promote, retire, set-parent) and the maps, seeds or
        usable submissions (a full rebuild). Cheap: a signature per submission."""
        a = self.arena
        stamp = self._stamp()
        if stamp != self.stamp or time.monotonic() - self.last_audit > AUDIT_SECONDS:
            self.rebuild()
            return
        changed = False
        with a.lock:
            # games added without record() (the server bridge's, imports, tests)
            for g in a.games[self._index_after(self.last_gid):]:
                self.record(g)
                changed = True
            for s in list(a.subs.values()):
                if self.sigs.get(s.id) != _sig(s):
                    self._load_sub(s)
                    self._settle(s.id)
                    changed = True
            for sid in [x for x in self.sigs if not dict.__contains__(a.subs, x)]:   # renamed or removed
                self._drop(sid)
                self.sigs.pop(sid, None)
                changed = True
        if changed:
            self._bump()

    def _pick(self, limit: int, remote: bool) -> list:
        """The next jobs in play order, without starting them (caller holds arena.lock)."""
        from arena import scheduler as S
        a = self.arena
        running = a.running
        busy = collections.Counter(r["job"].owner for r in running.values())
        created = {sid: s.created for sid, s in a.subs.items()}
        out, seen = [], set()
        heads: dict[str, int] = {}                 # sid -> index into its pending deque

        def head(sid):
            dq = self.pending.get(sid)
            if not dq:
                return None
            i = heads.get(sid, 0)
            while i < len(dq):
                j = dq[i]
                if (j.key in self.played or j.key in running or j.key in seen
                        or (remote and j.suite == "holdout")):
                    i += 1
                    continue
                heads[sid] = i
                return j
            heads[sid] = i
            return None

        while len(out) < limit:
            best = None
            for sid in self.pending:
                j = head(sid)
                if j is None:
                    continue
                k = (j.priority, busy[j.owner], created.get(j.for_sub, ""), j.for_sub, j.map, j.seed, j.key)
                if best is None or k < best[0]:
                    best = (k, sid, j)
            if best is None:
                break
            _, sid, j = best
            heads[sid] = heads.get(sid, 0) + 1
            seen.add(j.key)
            out.append(j)
            busy[j.owner] += 1
        if out or any(self.pending.get(sid) and head(sid) for sid in self.pending):
            return out
        # nothing owed: the top bots' holdout games, then background pairs
        taken = _Taken(self.played, running)
        extra = [] if remote else [j for j in S._holdout(a) if j.key not in taken]
        if not extra and a.cfg.get("play", "continuous", True):
            extra = [j for j in S._continuous(a, taken, self.met)
                     if not (remote and j.suite == "holdout")]
        while extra and len(out) < limit:          # the same play order as owed games
            extra.sort(key=lambda j: (j.priority, busy[j.owner], created.get(j.for_sub, ""), j.for_sub,
                                      j.map, j.seed, j.key))
            j = extra.pop(0)
            if j.key in seen:
                continue
            seen.add(j.key)
            out.append(j)
            busy[j.owner] += 1
        return out

    def peek(self, limit: int, remote: bool = False) -> list:
        with self.arena.lock:
            return self._pick(limit, remote)

    def take(self, limit: int, worker: str = "local", lease_of=None) -> list:
        """Picks up to `limit` jobs and marks them running on `worker` (atomically, so two
        callers never get the same game). `lease_of(job)` returns extra fields for the running
        entry (a remote worker's lease id)."""
        a = self.arena
        remote = worker != "local"
        with a.lock:
            jobs = self._pick(limit, remote)
            for j in jobs:
                a.running[j.key] = {"job": j, "started": now(), "worker": worker,
                                    **(lease_of(j) if lease_of else {})}
            if jobs:
                rs = self._running_sids()
                for sid in {sid for j in jobs for sid in self.owners.get(j.key, ())}:
                    self._settle(sid, rs)
                self._trim()
        if jobs:
            self._bump()
        return jobs

    def _trim(self):
        """Drops played and running games from the front of each deque (caller holds the lock)."""
        running = self.arena.running
        for sid, dq in self.pending.items():
            while dq and (dq[0].key in self.played or dq[0].key in running):
                dq.popleft()

    def _index_after(self, gid: int) -> int:
        """Position in arena.games of the first game with id > gid (ids count up)."""
        games = self.arena.games
        lo, hi = 0, len(games)
        while lo < hi:
            mid = (lo + hi) // 2
            if games[mid].id <= gid:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def record(self, g, job=None):
        """A finished game was added (a.add_game). Caller may or may not hold arena.lock."""
        a = self.arena
        key = (g.a, g.b, g.suite, g.map, g.seed)
        with a.lock:
            self.last_gid = max(self.last_gid, g.id)
            if job is not None:
                a.running.pop(job.key, None)        # the game and its result land in one step
            if g.seconds and not g.error and g.lane != "server":
                self.avg_s += (g.seconds - self.avg_s) / 200          # ~ the last few hundred games
            if current_game(g) and key not in self.played:
                self.played.add(key)
                if is_rated(g):
                    self.met[frozenset((g.a, g.b))] += 1
                for sid in list(self.owners.get(key, ())):
                    self.left[sid] = max(0, self.left.get(sid, 0) - 1)
            rs = self._running_sids()
            for sid in list(self.owners.get(key, ())):
                self._settle(sid, rs)
            self._trim()
        self._bump()

    def release(self, job, worker: str):
        """A started game that won't finish (an expired lease): owed again, at the front."""
        a = self.arena
        with a.lock:
            r = a.running.get(job.key)
            if not r or r.get("worker") != worker:
                return
            a.running.pop(job.key, None)
            for sid in self.owners.get(job.key, ()):
                if job.key not in self.played and sid in self.pending:
                    self.pending[sid].appendleft(self.jobs[sid][job.key])
        self._bump()

    def is_played(self, key) -> bool:
        return key in self.played

    # ------------------------------------------------------------------ what people see
    def view(self) -> dict:
        """What's running and what's left, built from memory (cached per version)."""
        v = self.version
        if self._view and self._view[0] == v:
            return self._view[1]
        a = self.arena
        d = getattr(a, "daemon", None)
        reg = getattr(a, "workers", None)
        with a.lock:
            running = [{"a": r["job"].a, "b": r["job"].b, "map": r["job"].map.rsplit("/", 1)[-1][:-4],
                        "suite": r["job"].suite, "seed": r["job"].seed, "lane": r["job"].lane,
                        "owner": r["job"].owner, "for": r["job"].for_sub, "started": r["started"],
                        "worker": r.get("worker", "local")}
                       for r in a.running.values()]
            waiting = {}
            by_lane: collections.Counter = collections.Counter()
            for sid, left in self.left.items():
                s = a.subs.get(sid)
                if not left or s is None:
                    continue
                total = len(self.jobs.get(sid, ()))
                waiting[sid] = {"owner": s.owner, "lane": s.lane, "status": s.status, "left": left,
                                "total": total, "games_left": left, "games_total": total,
                                "priority": next(iter(self.jobs[sid].values())).lane}
                by_lane[waiting[sid]["priority"]] += left
            checking = sorted(s.id for s in a.subs.values() if s.status == "checking")
        remote = reg.snapshot() if reg is not None else []
        local = d.workers if d is not None else 0
        out = {"version": v, "updated": now(), "live": True,
               "workers": local + sum(w["slots"] for w in remote),
               "slots": {"local": local, "remote": {w["name"]: w["slots"] for w in remote}},
               "remote": remote, "running": running, "checking": checking, "waiting": waiting,
               "totals": {"left": sum(by_lane.values()), "by_lane": dict(by_lane),
                          "running": len(running)},
               "avg_game_s": round(self.avg_s, 1)}
        self._view = (v, out)
        return out

    def wait(self, since: int, timeout: float) -> int:
        """Blocks until version > since (or timeout); returns the version."""
        with self.cond:
            if self.version <= since:
                self.cond.wait_for(lambda: self.version > since, timeout)
            return self.version

    def snapshot(self, path=None):
        """data_dir/queue.json for when the engine is down or restarting (written if changed)."""
        v = self.view()
        if getattr(self, "_written", None) == v["version"]:
            return
        store.write_json(path or self.arena.cfg.data_dir / "queue.json", v)
        self._written = v["version"]


def get(arena: Arena) -> GameQueue:
    """The arena's queue (created on first use)."""
    q = getattr(arena, "queue", None)
    if q is None:
        q = arena.queue = GameQueue(arena)
    return q
