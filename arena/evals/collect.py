"""The evals collector, `arena evals`: its own process (compose service `evals`).

Every [evals] poll_seconds (default 300) it reads the leaderboard and the recent battles of the
top [evals] top_teams teams (other than ours), downloads each finished battle it hasn't seen
and its games' replays, and computes every metric for each game in a small low-priority process
pool. Metrics that change version are recomputed from the kept replays when it next starts. The
report (arena/evals/report.py) is rebuilt whenever something new comes in.

It's polite with the shared key: one request every [evals] pace_seconds (default 3 s, so at most
20 a minute of the key's 120; the engine's bridge uses the rest), and it backs off on a 429.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from arena import metrics, store
from arena.evals import evals_dir, report
from arena.server.client import (DEFAULT_URL, Client, NoKey, RateLimited, ServerError, finished,
                                 load_key)

log = logging.getLogger("arena.evals")


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nice():
    try:
        os.nice(15)
    except OSError:
        pass


def _compute(path: str) -> dict:
    try:
        return metrics.compute(Path(path).read_bytes())
    except Exception as e:                               # a bad replay is recorded, not fatal
        return {"error": f"{type(e).__name__}: {e}"[:300]}


def _unwrap(x):
    return x[0] if isinstance(x, tuple) else x


class Collector:
    def __init__(self, cfg, client=None, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.dir = evals_dir(cfg)
        url = cfg.get("server", "url", DEFAULT_URL)
        self.client = client if client is not None else Client(load_key(cfg.get("server", "key_file"), url), url)
        g = lambda k, d: cfg.get("evals", k, d)
        self.pace = float(g("pace_seconds", 3))
        self.top = int(g("top_teams", 40))
        self.min_elo = float(g("min_elo", 0))
        self.poll_seconds = float(g("poll_seconds", 300))
        self.keep_bytes = float(g("keep_gb", 30)) * 1e9
        self.procs = int(g("procs", 2))
        self.ours = g("our_team_id", None)
        self.clock, self.sleep = clock, sleep
        self.pool: ProcessPoolExecutor | None = None
        self.futs: dict = {}                      # game id -> future
        self.stale: list[int] = []                # games whose metrics are an older version
        self.digests: dict[int, dict] = {}
        self.known: set[int] = set()              # game ids loaded or stored by this process
        self.dirty = True
        self._last_call = 0.0
        self.status = {"started": iso(clock()), "polls": 0, "last_poll": None, "last_error": None,
                       "new_games": 0, "requests": 0}

    def path(self, *parts) -> Path:
        return self.dir.joinpath(*map(str, parts))

    # ------------------------------------------------------------------ the server
    def call(self, fn, *args):
        """One paced request; waits out a 429 (three tries)."""
        for attempt in range(3):
            wait = self._last_call + self.pace - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last_call = self.clock()
            self.status["requests"] += 1
            try:
                return _unwrap(fn(*args))
            except RateLimited as e:
                log.info("rate limited; waiting %.0f s", e.retry_after)
                self.sleep(min(120.0, max(1.0, e.retry_after)))
        raise ServerError("still rate limited after 3 tries", 429)

    def our_team(self):
        if self.ours is None:
            try:
                t = self.call(self.client.team) or {}
                self.ours = t.get("id") or (t.get("team") or {}).get("id")
            except ServerError as e:
                log.warning("can't tell our team: %s", e)
        return self.ours

    def discover(self) -> dict[int, dict]:
        """Finished-or-not battles of the top teams that aren't stored yet: {id: listing}."""
        ours = self.our_team()
        rows = self.call(self.client.leaderboard) or []
        rows = rows if isinstance(rows, list) else (rows.get("teams") or [])
        rows = sorted((r for r in rows if r.get("id") != ours and (r.get("elo") or 0) >= self.min_elo),
                      key=lambda r: r.get("rank") or 10 ** 9)[:self.top]
        found: dict[int, dict] = {}
        for t in rows:
            prof = self.call(self.client.team_profile, t["id"]) or {}
            for b in prof.get("recent") or []:
                bid = b.get("id")
                if bid is None or b.get("opponentId") == ours or (b.get("opponentElo") or 0) < self.min_elo:
                    continue
                if bid in found or self.path("battles", f"{bid}.json").exists():
                    continue
                found[bid] = {"id": bid, "at": b.get("at"), "ranked": b.get("ranked"), "via": t.get("name"),
                              "a": t.get("name"), "b": b.get("opponent")}
        self.status["teams_polled"] = len(rows)
        return found

    def fetch(self, bid: int, listing: dict) -> bool:
        bt = self.call(self.client.battle, bid)
        if not finished(bt):
            return False
        self.ingest(bid, listing, bt, lambda gid: self.call(self.client.replay, gid))
        self.client.forget(f"battles/{bid}")
        return True

    # ------------------------------------------------------------------ storing
    def ingest(self, bid: int, listing: dict, bt: dict, blob_of) -> int:
        """Stores a finished battle's games (replay via blob_of(game id) unless kept) and queues
        their metrics. Returns how many new games."""
        m = bt.get("match") or {}
        teams = {"a": {"id": m.get("teamAId"), "name": bt.get("teamAName"), "elo": bt.get("teamAElo")},
                 "b": {"id": m.get("teamBId"), "name": bt.get("teamBName"), "elo": bt.get("teamBElo")}}
        new = 0
        for gm in bt.get("games") or []:
            gid = gm.get("id")
            if gid is None or gm.get("status") != "completed" or gm.get("hasReplay") is False:
                continue
            if self.path("games", f"{gid}.json").exists():
                continue
            rp = self.path("replays", f"{gid}.replay")
            if not rp.exists():
                blob = blob_of(gid)
                if not blob:
                    continue
                rp.parent.mkdir(parents=True, exist_ok=True)
                tmp = rp.with_suffix(".part")
                tmp.write_bytes(blob)
                os.replace(tmp, rp)
            info = {"id": gid, "battle": bid, "at": m.get("completedAt") or listing.get("at"),
                    "ranked": m.get("ranked", listing.get("ranked")),
                    "map": gm.get("mapName") or bt.get("mapName"), "teams": teams,
                    "team_winner": gm.get("winner"), "replay": rp.name, "seats": None}
            store.write_json(self.path("games", f"{gid}.json"), info)
            self.known.add(gid)
            self.submit(gid)
            new += 1
        store.write_json(self.path("battles", f"{bid}.json"), {"listing": listing, "battle": bt})
        self.status["new_games"] += new
        return new

    def submit(self, gid: int):
        path = str(self.path("replays", f"{gid}.replay"))
        if self.procs <= 0:
            self.finish(gid, _compute(path))
            return
        if self.pool is None:
            self.pool = ProcessPoolExecutor(self.procs, mp_context=mp.get_context("spawn"), initializer=_nice)
        self.futs[gid] = self.pool.submit(_compute, path)

    def finish(self, gid: int, m: dict):
        info = store.read_json(self.path("games", f"{gid}.json"), None)
        if info is None:
            return
        if not m.get("error"):
            w, tw = m["result"]["winner"], info.get("team_winner")
            if w in ("A", "B") and tw in ("a", "b"):
                a_seat = "A" if (w == "A") == (tw == "a") else "B"     # where team a sat
                t = info["teams"]
                info["seats"] = {a_seat: t["a"], "B" if a_seat == "A" else "A": t["b"]}
            info.update(winner=w, end=m["result"]["end"], rounds=(m.get("rounds") or [None])[-1])
            store.write_json(self.path("games", f"{gid}.json"), info)
        store.write_json(self.path("metrics", f"{gid}.json"), m)
        d = report.digest(info, m)
        if d:
            self.digests[gid] = d
            self.dirty = True

    def drain(self, wait: bool = False):
        for gid, f in list(self.futs.items()):
            if wait or f.done():
                self.futs.pop(gid)
                self.finish(gid, f.result())

    # ------------------------------------------------------------------ upkeep
    def load(self):
        """Everything stored, into digests; games with older metrics (and a replay) go stale.
        Run at start and after every poll, so games stored by `arena evals --import` join in."""
        new = 0
        for p in sorted(self.path("games").glob("*.json")):
            gid = int(p.stem)
            if gid in self.known or gid in self.futs:
                continue
            info = store.read_json(p, None)
            if not info:
                continue
            self.known.add(gid)
            new += 1
            m = store.read_json(self.path("metrics", f"{gid}.json"), None)
            if (m is None or metrics.outdated(m)) and self.path("replays", f"{gid}.replay").exists():
                self.stale.append(gid)
            if m and not m.get("error"):
                d = report.digest(info, m)
                if d:
                    self.digests[gid] = d
                    self.dirty = True
        self.stale.sort(reverse=True)                       # newest first
        if new:
            log.info("evals: %d games loaded, %d to (re)compute", new, len(self.stale))

    def recompute(self):
        room = max(1, self.procs) * 3 - len(self.futs)
        while room > 0 and self.stale:
            gid = self.stale.pop(0)
            if gid not in self.futs:
                self.submit(gid)
                room -= 1

    def retain(self):
        d = self.path("replays")
        if not d.exists():
            return
        files = sorted(d.glob("*.replay"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        self.status["replays_gb"] = round(total / 1e9, 2)
        if total <= self.keep_bytes:
            return
        for p in files:
            if total <= 0.9 * self.keep_bytes:
                break
            if int(p.stem) in self.futs:
                continue
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
        self.status["replays_gb"] = round(total / 1e9, 2)

    def write_report(self):
        if not self.dirty:
            return
        rep = report.build(list(self.digests.values()), metrics.versions(), metrics.describe(),
                           now=self.clock())
        store.write_json(self.path("report.json"), rep)
        self.dirty = False

    def write_status(self):
        self.status.update(updated=iso(self.clock()), games=len(self.digests), pending=len(self.futs),
                           stale=len(self.stale), poll_seconds=self.poll_seconds, top_teams=self.top,
                           battles=sum(1 for _ in self.path("battles").glob("*.json")) if self.path("battles").exists() else 0)
        store.write_json(self.path("status.json"), self.status)

    def upkeep(self):
        self.drain()
        self.recompute()
        self.write_report()
        self.write_status()

    # ------------------------------------------------------------------ the loop
    def poll(self):
        found = self.discover()
        n = 0
        for bid, listing in sorted(found.items()):
            try:
                n += self.fetch(bid, listing)
            except ServerError as e:
                log.warning("battle %s: %s", bid, e)
            self.drain()
        self.status.update(last_poll=iso(self.clock()), polls=self.status["polls"] + 1, last_found=len(found))
        log.info("evals: poll found %d new battles, stored %d", len(found), n)

    def run_forever(self):
        self.load()
        while True:
            t0 = self.clock()
            try:
                self.poll()
                self.status["last_error"] = None
            except NoKey:
                self.status["last_error"] = "no server key configured ([server] key_file)"
            except Exception as e:                       # keep running; say what went wrong
                log.exception("evals poll failed")
                self.status["last_error"] = f"{type(e).__name__}: {e}"[:300]
            self.retain()
            self.load()
            while True:
                self.upkeep()
                if self.clock() >= t0 + self.poll_seconds:
                    break
                self.sleep(5)


def import_dir(cfg, folder: Path) -> int:
    """Stores battles crawled earlier: <folder>/M<battle>/battle.json ({listing, battle}) with
    M<game>.replay files beside it. Computes their metrics, then writes the report."""
    c = Collector(cfg, client=False)
    c.load()
    new = 0
    for bj in sorted(folder.glob("M*/battle.json")):
        d = store.read_json(bj, {}) or {}
        bt, listing = d.get("battle"), d.get("listing") or {}
        if not isinstance(bt, dict) or not finished(bt):
            continue
        bid = listing.get("id") or (bt.get("match") or {}).get("id")

        def blob_of(gid, base=bj.parent):
            p = base / f"M{gid}.replay"
            return p.read_bytes() if p.exists() else None
        new += c.ingest(bid, listing, bt, blob_of)
        c.drain()
    c.drain(wait=True)
    c.write_report()
    c.write_status()
    return new


def main(args) -> int:
    from arena import config
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    if getattr(args, "import_dir", None):
        n = import_dir(cfg, Path(args.import_dir))
        print(f"imported {n} games into {evals_dir(cfg)}")
        return 0
    Collector(cfg).run_forever()
    return 0

