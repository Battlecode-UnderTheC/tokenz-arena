"""Duels and ephemeral bots (docs/DUELS.md): ad-hoc A-vs-B tests on fresh seeds that never touch
the ladder.

An ephemeral bot is a zip checked like a submission (unpacked, no environment reads, compiled
with the judge's clang) but kept out of arena.subs: no ratings, ideas, lineage or leaderboard.
It lives in data_dir/ephemeral/<id>/ and is unpacked to bots/<id> so judges and workers find it
the way they find a submission. It expires after its TTL.

A duel is a schedule of games between two bots (submissions or ephemeral bots) on the maps of
the suites asked for, pinned when the duel is created (copied into its folder, so a new rotating
epoch can't take them away), on fresh random seeds by default. Its games are played like any
other (local judges and leased to workers, cppjudge, the scaler sees them as backlog) but are
recorded here, in data_dir/duels/<id>/, never as arena Games: nothing that feeds ratings,
verdicts, the auto-submit gate or the evals can see them.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import random
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena import store
from arena.state import EXTERNAL, REJECTED, Arena, now

log = logging.getLogger("arena")

SUITES = ("official", "tournament", "server", "custom", "rotating")
PRIORITY = 2                    # after drafts (0) and placement (1), before rated top-up (3) and filler
TTL_HOURS = 24


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------- ephemeral bots
@dataclass
class Ephemeral:
    id: str
    name: str
    owner: str
    sha256: str
    expires: str
    status: str = "checking"         # checking -> ready | rejected
    created: str = field(default_factory=now)
    agent: str | None = None
    language: str | None = None
    reason: str | None = None
    check_log: str | None = None
    lane: str = "ephemeral"


def is_ephemeral(bot_id: str) -> bool:
    return bot_id.startswith("eph-")


# ---------------------------------------------------------------------- duels
@dataclass
class Duel:
    id: str
    owner: str
    a: str
    b: str
    suites: list
    seeds: str                        # "random:N" or "list"
    games_planned: int
    both_seats: bool = True
    created: str = field(default_factory=now)
    status: str = "waiting"           # waiting (a bot is checking) -> queued -> playing -> done | cancelled | failed
    agent: str | None = None
    note: str = ""
    reason: str | None = None
    finished: str | None = None
    # n -> {suite, map (stem), path (pinned map file), seed, swapped (True: b sits in seat A)}
    schedule: list = field(default_factory=list)


class DuelError(ValueError):
    pass


class Book:
    """Every duel and ephemeral bot, in memory, persisted under data_dir. The queue asks it for
    pending duel games and hands it their results."""

    def __init__(self, arena: Arena):
        self.arena = arena
        self.lock = threading.RLock()
        self.dir = arena.cfg.data_dir / "duels"
        self.edir = arena.cfg.data_dir / "ephemeral"
        self.ephemeral: dict[str, Ephemeral] = {}
        self.duels: dict[str, Duel] = {}
        self.done: dict[str, set] = {}            # duel -> game numbers recorded
        self.pending: dict[str, collections.deque] = {}
        self.version = 0
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self):
        for p in sorted(self.edir.glob("*/meta.json")) if self.edir.is_dir() else []:
            d = store.read_json(p, None)
            if d:
                e = Ephemeral(**{k: v for k, v in d.items() if k in Ephemeral.__dataclass_fields__})
                self.ephemeral[e.id] = e
        for p in sorted(self.dir.glob("*/meta.json")) if self.dir.is_dir() else []:
            d = store.read_json(p, None)
            if not d:
                continue
            u = Duel(**{k: v for k, v in d.items() if k in Duel.__dataclass_fields__})
            self.duels[u.id] = u
            self.done[u.id] = {int(f.stem) for f in (p.parent / "games").glob("*.json")}
            self._fill(u)

    def _save_eph(self, e: Ephemeral):
        store.write_json(self.edir / e.id / "meta.json", asdict(e))

    def _save_duel(self, u: Duel):
        store.write_json(self.dir / u.id / "meta.json", asdict(u))

    # ------------------------------------------------------------------ bots
    def bot(self, bot_id: str):
        """A submission or an ephemeral bot, by id (None if neither). An ephemeral bot not in
        memory is read from disk (the website's replica serves worker downloads)."""
        if not is_ephemeral(bot_id):
            return self.arena.subs.get(bot_id)
        e = self.ephemeral.get(bot_id)
        if e is None:
            d = store.read_json(self.edir / Path(bot_id).name / "meta.json", None)
            if d:
                e = Ephemeral(**{k: v for k, v in d.items() if k in Ephemeral.__dataclass_fields__})
        return e

    def bot_zip(self, bot_id: str) -> Path:
        if is_ephemeral(bot_id):
            return self.edir / bot_id / "source.zip"
        return self.arena.cfg.submissions / bot_id / "source.zip"

    def ready(self, bot_id: str) -> bool | None:
        """True: playable; None: still checking; False: unusable (rejected, unknown, external)."""
        b = self.bot(bot_id)
        if b is None:
            return False
        if b.status in (REJECTED, EXTERNAL):
            return False
        if b.status == "checking":
            return None
        return (self.arena.cfg.bots / bot_id).is_dir()

    def add_ephemeral(self, blob: bytes, name: str, owner: str, ttl_hours: float, agent: str | None) -> tuple[Ephemeral, bool]:
        from arena import submit
        submit._validate_zip(blob)
        sha = hashlib.sha256(blob).hexdigest()
        ttl = max(0.1, float(ttl_hours or TTL_HOURS))
        expires = _iso(datetime.now(timezone.utc) + timedelta(hours=ttl))
        with self.lock:
            for e in self.ephemeral.values():          # the same zip from the same owner: renew it
                if e.sha256 == sha and e.owner == owner and e.status != REJECTED:
                    e.expires = max(e.expires, expires)
                    self._save_eph(e)
                    return e, False
            eid = f"eph-{secrets.token_hex(4)}"
            e = Ephemeral(id=eid, name=(name or "bot")[:48], owner=owner, sha256=sha, expires=expires, agent=agent)
            (self.edir / eid).mkdir(parents=True, exist_ok=True)
            (self.edir / eid / "source.zip").write_bytes(blob)
            self.ephemeral[eid] = e
            self._save_eph(e)
            self.version += 1
            return e, True

    def check_ephemeral(self, e: Ephemeral) -> bool:
        """Unpack, no environment reads, compile with the judge's clang (no check game: the duel's
        own games are the test). Slow: a build."""
        from arena import runner, submit
        cfg = self.arena.cfg
        try:
            root = submit._unpack((self.edir / e.id / "source.zip").read_bytes(), cfg.bots / e.id)
            e.language = submit._language(root)
            if hit := submit._env_reads(root, e.language):
                raise submit.Rejected(f"reads environment variables (not allowed by the competition): {hit}")
            runner.build_wasm(cfg.bots / e.id)
        except Exception as ex:
            said = str(ex).strip()
            with self.lock:
                e.status, e.reason = REJECTED, (said.splitlines()[-1] if said else type(ex).__name__)[:300]
                e.check_log = said[-4000:]
                self._save_eph(e)
            self._refill_all()
            return False
        with self.lock:
            e.status = "ready"
            self._save_eph(e)
        self._refill_all()
        return True

    def remove_ephemeral(self, eid: str):
        with self.lock:
            e = self.ephemeral.pop(eid, None)
        if e is None:
            return
        shutil.rmtree(self.edir / eid, ignore_errors=True)
        shutil.rmtree(self.arena.cfg.bots / eid, ignore_errors=True)
        self._refill_all()

    # ------------------------------------------------------------------ duels
    def create(self, *, owner: str, a: str, b: str, suites: list | None = None, maps: list | None = None,
               seeds="random:4", games: int | None = None, both_seats: bool = True,
               agent: str | None = None, note: str = "") -> Duel:
        from arena import scheduler as S
        if a == b:
            raise DuelError("a duel needs two different bots")
        for x in (a, b):
            if self.ready(x) is False:
                raise DuelError(f"{x!r} isn't a playable submission or ephemeral bot")
        suites = list(dict.fromkeys(suites or ["official", "tournament", "server"]))
        bad = [s for s in suites if s not in SUITES]
        if bad:
            raise DuelError(f"suites must be among {', '.join(SUITES)} (holdout maps never leave the arena)")
        want = {m.lower() for m in maps} if maps else None
        files = []
        for suite in suites:
            for p in S.maps(self.arena, suite):
                if want is None or p.stem.lower() in want:
                    files.append((suite, p))
        if not files:
            raise DuelError("no maps match those suites and names")
        if isinstance(seeds, str) and seeds.startswith("random:"):
            per_map = int(seeds.split(":", 1)[1])
            if per_map < 1:
                raise DuelError("random:N needs N >= 1")
            rounds = [[secrets.randbits(63) for _ in files] for _ in range(per_map)]
            seed_label = seeds
        else:
            lst = [int(x) for x in (seeds if isinstance(seeds, list) else str(seeds).split(","))]
            if not lst:
                raise DuelError("seeds: 'random:N' or a list of integers")
            rounds = [[s] * len(files) for s in lst]
            seed_label = ",".join(map(str, lst))
        did = f"duel-{secrets.token_hex(4)}"
        pin = self.dir / did / "maps"
        pinned = {}
        for suite, p in files:
            dest = pin / suite / p.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            pinned[(suite, p.stem)] = dest
        rng = random.Random(did)
        sched = []
        for r in rounds:
            order = list(range(len(files)))
            rng.shuffle(order)
            for i in order:
                suite, p = files[i]
                for swapped in ((False, True) if both_seats else (False,)):
                    sched.append({"suite": suite, "map": p.stem, "path": str(pinned[(suite, p.stem)]),
                                  "seed": r[i], "swapped": swapped})
        if games:
            sched = sched[:max(1, int(games))]
        u = Duel(id=did, owner=owner, a=a, b=b, suites=suites, seeds=seed_label, games_planned=len(sched),
                 both_seats=both_seats, agent=agent, note=(note or "")[:300], schedule=sched)
        with self.lock:
            self.duels[did] = u
            self.done[did] = set()
            self._fill(u)
            self._save_duel(u)
            self.version += 1
        return u

    def cancel(self, did: str, reason: str = "cancelled"):
        with self.lock:
            u = self.duels.get(did)
            if u is None or u.status in ("done", "cancelled", "failed"):
                return u
            u.status, u.reason, u.finished = "cancelled", reason, now()
            self.pending.pop(did, None)
            self._save_duel(u)
            self.version += 1
            return u

    def _fill(self, u: Duel):
        """u's pending games (caller holds the lock): none until both bots are ready."""
        from arena.scheduler import Job
        if u.status in ("done", "cancelled", "failed"):
            self.pending.pop(u.id, None)
            return
        ra, rb = self.ready(u.a), self.ready(u.b)
        if ra is False or rb is False:
            u.status, u.reason, u.finished = "failed", "a bot became unusable (rejected or removed)", now()
            self.pending.pop(u.id, None)
            self._save_duel(u)
            return
        if ra is None or rb is None:
            u.status = "waiting"
            self.pending[u.id] = collections.deque()
            return
        if u.status == "waiting":
            u.status = "queued"
            self._save_duel(u)
        done = self.done.get(u.id, set())
        self.pending[u.id] = collections.deque(
            Job(PRIORITY, u.b if g["swapped"] else u.a, u.a if g["swapped"] else u.b, g["suite"], g["path"],
                g["seed"], u.owner, u.id, duel=(u.id, n))
            for n, g in enumerate(u.schedule) if n not in done)

    def _refill_all(self):
        with self.lock:
            for u in self.duels.values():
                if u.status == "waiting":
                    self._fill(u)
            self.version += 1
        q = getattr(self.arena, "queue", None)
        if q is not None:
            q._bump()

    def release(self, job):
        """A leased duel game that won't finish: pending again, first in line."""
        did, n = job.duel
        with self.lock:
            u = self.duels.get(did)
            if u and u.status not in ("done", "cancelled", "failed") and n not in self.done.get(did, set()):
                self.pending.setdefault(did, collections.deque()).appendleft(job)

    def started(self, did: str):
        with self.lock:
            u = self.duels.get(did)
            if u and u.status == "queued":
                u.status = "playing"
                self._save_duel(u)

    def record(self, job, res: dict, worker: str, replay: Path | None) -> bool:
        """A finished duel game: stored under the duel, never as an arena Game. False if it was
        already recorded (a duplicate) or the duel is gone."""
        from arena import retention
        did, n = job.duel
        with self.lock:
            u = self.duels.get(did)
            if u is None or n in self.done.get(did, set()):
                return False
            self.done.setdefault(did, set()).add(n)
        d = self.dir / did
        g = u.schedule[n]
        out = {"n": n, "suite": g["suite"], "map": g["map"], "seed": g["seed"], "seat_a": job.a, "seat_b": job.b,
               "swapped": g["swapped"], "worker": worker, "at": now()}
        if res.get("error"):
            out["error"] = res["error"]
        else:
            out.update(winner=res.get("winner"), rounds=res.get("rounds"), reason=res.get("reason"),
                       seconds=res.get("seconds"), stats=res.get("stats") or {}, judge=res.get("judge", "official"))
            if res.get("metrics"):
                store.write_json(d / "metrics" / f"{n}.json", res["metrics"])
            if replay is not None and Path(replay).exists():
                dest = d / "replays" / f"{n}.replay"
                dest.parent.mkdir(parents=True, exist_ok=True)
                if retention.is_gzip(Path(replay)):
                    shutil.move(str(replay), dest)
                else:
                    retention.compress_file(Path(replay), dest)
                out["replay"] = True
        store.write_json(d / "games" / f"{n}.json", out)
        with self.lock:
            if len(self.done[did]) >= len(u.schedule) and u.status not in ("cancelled", "failed"):
                u.status, u.finished = "done", now()
                self.pending.pop(did, None)
            self._save_duel(u)
            self.version += 1
        return True

    def left(self, did: str) -> int:
        u = self.duels.get(did)
        if u is None or u.status in ("done", "cancelled", "failed"):
            return 0
        return len(u.schedule) - len(self.done.get(did, set()))

    # ------------------------------------------------------------------ upkeep
    def expire(self):
        """Removes expired ephemeral bots not in an unfinished duel, and old duels' files:
        replays after [duels] replay_days (3), everything after keep_days (30)."""
        t = datetime.now(timezone.utc)
        with self.lock:
            busy = {x for u in self.duels.values() if u.status in ("waiting", "queued", "playing") for x in (u.a, u.b)}
            gone = [e.id for e in self.ephemeral.values() if _parse(e.expires) < t and e.id not in busy]
        for eid in gone:
            self.remove_ephemeral(eid)
            log.info("duels: ephemeral bot %s expired", eid)
        rd = float(self.arena.cfg.get("duels", "replay_days", 3))
        kd = float(self.arena.cfg.get("duels", "keep_days", 30))
        with self.lock:
            old = [(u.id, _parse(u.finished)) for u in self.duels.values() if u.finished]
        for did, fin in old:
            if t - fin > timedelta(days=kd):
                with self.lock:
                    self.duels.pop(did, None)
                    self.done.pop(did, None)
                    self.pending.pop(did, None)
                shutil.rmtree(self.dir / did, ignore_errors=True)
            elif t - fin > timedelta(days=rd):
                shutil.rmtree(self.dir / did / "replays", ignore_errors=True)
                shutil.rmtree(self.dir / did / "maps", ignore_errors=True)

    # ------------------------------------------------------------------ reading
    def games(self, did: str) -> list[dict]:
        d = self.dir / did / "games"
        out = [store.read_json(p, None) for p in d.glob("*.json")] if d.is_dir() else []
        return sorted((g for g in out if g), key=lambda g: g["n"])

    def summary(self, did: str, detail: bool = True) -> dict:
        """The duel from A's side: status, record with a 95% interval, by suite, map, end and
        starting dragons, both sides' metrics, and each game."""
        from arena import report
        with self.lock:
            u = self.duels[did]
            meta = asdict(u)
        meta.pop("schedule")
        running = sum(1 for r in self.arena.running.values() if getattr(r["job"], "duel", None) and r["job"].duel[0] == did)
        games = self.games(did)
        played = [g for g in games if not g.get("error")]
        out = {**meta, "games_left": self.left(did), "games_played": len(played), "games_running": running,
               "errors": len(games) - len(played)}

        def a_result(g):
            if g.get("winner") is None:
                return "D"
            a_seat = "B" if g["swapped"] else "A"
            return "W" if g["winner"] == a_seat else "L"

        def row(gs):
            r = collections.Counter(a_result(g) for g in gs)
            return report._row({"W": r["W"], "D": r["D"], "L": r["L"]})

        out["record"] = row(played)
        groups = {"by_suite": lambda g: g["suite"], "by_map": lambda g: f"{g['suite']}/{g['map']}",
                  "by_end": lambda g: "draw" if g.get("winner") is None else (g.get("reason") or "?").split(",")[0].strip(),
                  "by_starters": lambda g: _starters(u.schedule[g["n"]]["path"])}
        for k, fn in groups.items():
            b = collections.defaultdict(list)
            for g in played:
                b[fn(g)].append(g)
            out[k] = {key: row(v) for key, v in sorted(b.items())}
        out["sides"] = _sides(self.dir / did, u, played)
        if detail:
            out["games"] = [{"n": g["n"], "suite": g["suite"], "map": g["map"], "seed": g["seed"],
                             "seat_a": g["seat_a"], "seat_b": g["seat_b"], "result_for_a": None if g.get("error") else a_result(g),
                             "winner_seat": g.get("winner"), "rounds": g.get("rounds"), "reason": g.get("reason"),
                             "error": g.get("error"), "judge": g.get("judge"),
                             "replay": f"/api/v1/duels/{did}/games/{g['n']}/replay" if g.get("replay") else None,
                             "watch": f"/duels/{did}/watch/{g['n']}" if g.get("replay") else None}
                            for g in games]
        return out


_STARTERS: dict = {}


def _starters(path: str) -> str:
    """'≤2' or '3+' starting dragons per side (or 'mixed'), from the pinned map's DRAGON lines."""
    if path not in _STARTERS:
        n = collections.Counter()
        try:
            for line in Path(path).read_text().splitlines():
                p = line.split()
                if p and p[0] in ("DRAGON", "SNAKE"):
                    n[p[1]] += 1
        except OSError:
            pass
        sides = {"≤2" if c <= 2 else "3+" for c in n.values()} or {"?"}
        _STARTERS[path] = sides.pop() if len(sides) == 1 else "mixed"
    return _STARTERS[path]


SIDE_ROUNDS = (100, 200, 300, 400)
SIDE_METRICS = ("dragons", "total_length", "longest", "board_control", "pearl_rate", "queen_alive",
                "queen_length", "tiles_visited", "tiles_seen")


def _sides(d: Path, u: Duel, played: list[dict]) -> dict:
    """Both bots' metrics, averaged at rounds 100-400 and at the end, from their own seat; and
    their queens, deaths, errors and CPU."""
    from arena.metrics import QUEEN_CAUSES
    acc = {x: collections.defaultdict(list) for x in ("a", "b")}
    queens = {x: {"died": 0, "rounds": [], "causes": collections.Counter()} for x in ("a", "b")}
    deaths = {x: collections.Counter() for x in ("a", "b")}
    cpu = {x: {"p99": [], "max": [], "overruns": 0} for x in ("a", "b")}
    for g in played:
        seat = {"a": "B" if g["swapped"] else "A", "b": "A" if g["swapped"] else "B"}
        teams = (g.get("stats") or {}).get("teams") or {}
        for x in ("a", "b"):
            t = teams.get(seat[x]) or {}
            deaths[x].update(t.get("deaths") or {})
            pts = t.get("points") or {}
            if pts:
                cpu[x]["p99"].append(pts.get("p99", 0))
                cpu[x]["max"].append(pts.get("max", 0))
            cpu[x]["overruns"] += t.get("cpu_overruns") or 0
        m = store.read_json(d / "metrics" / f"{g['n']}.json", None)
        if not m or m.get("error"):
            continue
        idx = {r: i for i, r in enumerate(m.get("rounds") or [])}
        for x in ("a", "b"):
            for k in SIDE_METRICS:
                series = ((m.get("team") or {}).get(k) or {}).get(seat[x])
                if not series:
                    continue
                for r in SIDE_ROUNDS:
                    if r in idx and series[idx[r]] is not None:
                        acc[x][f"{k}@{r}"].append(series[idx[r]])
                if series[-1] is not None:
                    acc[x][f"{k}@end"].append(series[-1])
            q = (m.get("queens") or {}).get(seat[x])
            if q:
                queens[x]["died"] += 1
                queens[x]["rounds"].append(q.get("round"))
                queens[x]["causes"][q.get("cause")] += 1
    out = {}
    n = len(played)
    for x, bot in (("a", u.a), ("b", u.b)):
        mean = {k: round(sum(v) / len(v), 4) for k, v in sorted(acc[x].items()) if v}
        q = queens[x]
        out[x] = {"bot": bot, "metrics": mean,
                  "queen": {"alive_at_end_pct": round(100 * (n - q["died"]) / n, 1) if n else None,
                            "median_death_round": sorted(q["rounds"])[len(q["rounds"]) // 2] if q["rounds"] else None,
                            "causes": {c: q["causes"][c] for c in QUEEN_CAUSES if q["causes"].get(c)}},
                  "deaths": dict(deaths[x].most_common()),
                  "cpu": {"p99_max": max(cpu[x]["p99"], default=None), "max": max(cpu[x]["max"], default=None),
                          "overruns": cpu[x]["overruns"]}}
    return out


def get(arena: Arena) -> Book:
    b = getattr(arena, "duels", None)
    if b is None:
        b = arena.duels = Book(arena)
    return b
