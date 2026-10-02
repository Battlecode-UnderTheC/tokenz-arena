"""Picks the next games (DESIGN.md section 3). Priority: drafts > placement > holdout >
continuous, and within a priority the owner with the fewest running games goes first.

A job is one game: (a, b, suite, map, seed). A job is owed until a game with that key
exists or is running. Every pairing is played from both seats.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from arena import store
from arena.state import current_game, is_rated, DONE, PLACING, PLAYING, QUEUED, RATED, REJECTED, EXTERNAL, Arena

DRAFT, PLACEMENT, HOLDOUT, CONTINUOUS = 0, 1, 2, 3
LANES = {DRAFT: "draft", PLACEMENT: "placement", HOLDOUT: "holdout", CONTINUOUS: "continuous"}


@dataclass(frozen=True)
class Job:
    priority: int
    a: str
    b: str
    suite: str
    map: str            # path to the .map file
    seed: int
    owner: str
    for_sub: str        # the submission this game is owed to

    @property
    def key(self) -> tuple:
        return (self.a, self.b, self.suite, Path(self.map).stem, self.seed)

    @property
    def lane(self) -> str:
        return LANES[self.priority]


_MAPS: dict = {}


def _dir_stamp(arena: Arena, suite: str) -> tuple:
    """Modification times of the folders a suite's listing depends on (adding or removing a
    map changes its folder's mtime); the server suite also depends on the server folder."""
    names = {"holdout": ["holdout", "holdout_custom"], "official": ["official", "server"],
             "tournament": ["tournament", "server"]}.get(suite, [suite])
    out = []
    for n in names:
        d = arena.cfg.map_dir(n)
        try:
            out.append((str(d), d.stat().st_mtime_ns))
        except (OSError, AttributeError):
            out.append((str(d), None))
    return tuple(out)


def maps(arena: Arena, suite: str) -> list[Path]:
    """A suite's maps; re-listed only when one of its folders changes (listing reads every
    map file's header)."""
    from arena.maps import suite_files
    key, stamp = (id(arena.cfg), suite), _dir_stamp(arena, suite)
    hit = _MAPS.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    files = suite_files(arena.cfg, suite)
    _MAPS[key] = (stamp, files)
    return files


def ranking(arena: Arena) -> list[str]:
    """Current ranked submissions, best first (rated ones by Elo, then the rest)."""
    ratings = getattr(arena, "ratings", {}) or {}
    cur = arena.current()
    return [s.id for s in sorted(cur, key=lambda s: (-(ratings.get(s.id, {}).get("elo") or -1e9),
                                                     s.created))]


_SEEDS: dict = {"mtime": None, "seeds": {}}


def server_seeds(arena: Arena) -> dict[str, list[int]]:
    """{map stem: seeds seen on the competition server}, written by the server bridge
    (data_dir/server/seeds.json); re-read only when the file changes."""
    p = arena.cfg.data_dir / "server" / "seeds.json"
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _SEEDS["mtime"] != (p, mtime):
        _SEEDS.update(mtime=(p, mtime), seeds=store.read_json(p, {}) or {})
    return _SEEDS["seeds"]


def _usable(arena: Arena, sid: str | None) -> bool:
    s = arena.subs.get(sid or "")
    return bool(s) and s.status not in (REJECTED, EXTERNAL) and (arena.cfg.bots / s.id).exists()


def _pairings(arena: Arena, sub: str, opponents: list[str], suite: str, seeds: list[int],
              priority: int, owner: str) -> list[Job]:
    out = []
    for opp in dict.fromkeys(opponents):
        if opp == sub or not _usable(arena, opp):
            continue
        for m in maps(arena, suite):
            for seed in seeds:
                out.append(Job(priority, sub, opp, suite, str(m), seed, owner, sub))
                out.append(Job(priority, opp, sub, suite, str(m), seed, owner, sub))
    return out


DRAFT_SUITES = ["official", "tournament", "server", "custom"]
PLACEMENT_SUITES = ["official", "tournament", "server", "custom", "rotating"]
CONTINUOUS_SUITES = ["official", "tournament", "custom", "rotating"]


def suite_seeds(arena: Arena, suite: str, m: Path) -> list[int]:
    """Seeds for one map: the server suite uses the seeds seen on the competition server for
    that map (the first [server] suite_seeds of them); other suites the first configured seed."""
    if suite == "server":
        per_map = arena.cfg.get("server", "suite_seeds", 2)
        seen = server_seeds(arena).get(m.stem, [])[:per_map]
        if seen:
            return seen
    return arena.cfg.seeds(suite)[:1]


def _suite_jobs(arena: Arena, s, opponents: list[str], suites: list[str], priority: int) -> list[Job]:
    jobs = []
    for suite in suites:
        for m in maps(arena, suite):
            for seed in suite_seeds(arena, suite, m):
                for opp in dict.fromkeys(opponents):
                    if opp == s.id or not _usable(arena, opp):
                        continue
                    jobs.append(Job(priority, s.id, opp, suite, str(m), seed, s.owner, s.id))
                    jobs.append(Job(priority, opp, s.id, suite, str(m), seed, s.owner, s.id))
    return jobs


_OWED: dict = {}


def owed_for(arena: Arena, s) -> list[Job]:
    """owed_games(), cached: the set only changes with the submission's status, lane, parent
    and opponents, or with the map pools and server seeds (re-checked every 10 s), so the
    scheduler doesn't rebuild thousands of jobs per submission on every tick."""
    if s.status not in (QUEUED, PLAYING, DONE, PLACING, RATED):
        return []
    suites = tuple((x, tuple(maps(arena, x))) for x in
                   ("official", "tournament", "server", "custom", "rotating"))
    key = (s.status, s.lane, s.parent, tuple(s.opponents or ()) if s.opponents is not None else None,
           tuple(s.quick_opponents or ()) if s.quick_opponents is not None else None,
           suites, tuple(sorted((k, tuple(v)) for k, v in server_seeds(arena).items())))
    hit = _OWED.get((id(arena), s.id))
    if hit and hit[0] == key and time.monotonic() - hit[1] < 60:
        return hit[2]
    jobs = owed_games(arena, s)
    # freezing opponents inside owed_games changes the key; store under the new one
    key = key[:3] + (tuple(s.opponents or ()) if s.opponents is not None else None,
                     tuple(s.quick_opponents or ()) if s.quick_opponents is not None else None) + key[5:]
    _OWED[(id(arena), s.id)] = (key, time.monotonic(), jobs)
    return jobs


def owed_games(arena: Arena, s) -> list[Job]:
    """Every game submission s is still owed (its draft or placement set), played or not.

    Drafts play their parent and the top bots on [play] draft_suites (default: official,
    tournament and server maps); ranked submissions play everyone on [play] placement_suites
    (default adds this week's rotating worldgen maps). The server suite, the maps and seeds the
    competition server actually uses, means a bot that only wins on our maps shows it before
    it goes live."""
    if s.lane == "draft" and s.status in (QUEUED, PLAYING, DONE):
        if s.opponents is None:
            top_n = arena.cfg.get("play", "draft_top_n", 3)
            top = [x for x in ranking(arena) if x not in (s.parent, s.id)][:top_n]
            _freeze(arena, s, ([s.parent] if s.parent else []) + top)
        return _suite_jobs(arena, s, s.opponents, arena.cfg.get("play", "draft_suites", DRAFT_SUITES),
                           DRAFT)
    if s.status in (PLACING, RATED):
        if s.status == PLACING and s.opponents is None:
            top_n = arena.cfg.get("play", "placement_quick_top", 5)
            quick = [x for x in ranking(arena) if x not in (s.parent, s.id)][:top_n]
            s.quick_opponents = [o for o in dict.fromkeys(([s.parent] if s.parent else []) + quick)
                                 if o != s.id]
            _freeze(arena, s, ([s.parent] if s.parent else []) + [c.id for c in arena.current()])
        if s.status == PLACING:
            # Quick placement: parent and the top bots on the draft maps, at placement priority.
            # Once those are played it's rated, and the rest (everyone, every placement map)
            # tops up below drafts, so a new ranked bot never blocks the queue.
            return _suite_jobs(arena, s, s.quick_opponents if s.quick_opponents is not None
                               else s.opponents or [],
                               arena.cfg.get("play", "draft_suites", DRAFT_SUITES), PLACEMENT)
        return _suite_jobs(arena, s, s.opponents or [],
                           arena.cfg.get("play", "placement_suites", PLACEMENT_SUITES), CONTINUOUS)
    return []


def _freeze(arena: Arena, s, opponents: list[str]):
    s.opponents = [o for o in dict.fromkeys(opponents) if o != s.id]
    arena.save_sub(s)


def _holdout(arena: Arena) -> list[Job]:
    top = ranking(arena)[:4]
    seeds = arena.cfg.seeds("holdout")
    out = []
    for i, a in enumerate(top):
        out += _pairings(arena, a, top[i + 1:], "holdout", seeds, HOLDOUT, "arena")
    return out


def _continuous(arena: Arena, taken: set) -> list[Job]:
    cur = ranking(arena)
    if len(cur) < 2:
        return []
    ratings = getattr(arena, "ratings", {}) or {}
    met: dict[frozenset, int] = {}
    for g in arena.games:
        if is_rated(g):
            k = frozenset((g.a, g.b))
            met[k] = met.get(k, 0) + 1
    pairs = []
    for i, a in enumerate(cur):
        for b in cur[i + 1:]:
            gap = abs((ratings.get(a, {}).get("elo") or 1500) - (ratings.get(b, {}).get("elo") or 1500))
            pairs.append((met.get(frozenset((a, b)), 0) + gap / 50, a, b))
    for _, a, b in sorted(pairs):
        for suite in arena.cfg.get("play", "continuous_suites", CONTINUOUS_SUITES):
            for seed in arena.cfg.seeds(suite):
                jobs = [j for j in _pairings(arena, a, [b], suite, [seed], CONTINUOUS, "arena")
                        if j.key not in taken]
                if jobs:
                    return jobs
    return []


def next_jobs(arena: Arena, limit: int) -> list[Job]:
    """Up to `limit` jobs to start now, in priority and fair-share order."""
    with arena.lock:
        taken = {(g.a, g.b, g.suite, g.map, g.seed) for g in arena.games if current_game(g)}
        taken |= set(arena.running)
        owed = []
        for s in arena.subs.values():
            owed += [j for j in owed_for(arena, s) if j.key not in taken]
        if not owed:
            owed = [j for j in _holdout(arena) if j.key not in taken]
        if not owed and arena.cfg.get("play", "continuous", True):
            owed = _continuous(arena, taken)
        busy: dict[str, int] = {}
        for r in arena.running.values():
            busy[r["job"].owner] = busy.get(r["job"].owner, 0) + 1
        created = {sid: s.created for sid, s in arena.subs.items()}
        out, seen = [], set()
        while owed and len(out) < limit:
            owed.sort(key=lambda j: (j.priority, busy.get(j.owner, 0), created.get(j.for_sub, ""),
                                     j.map, j.seed))
            j = owed.pop(0)
            if j.key in seen:
                continue
            seen.add(j.key)
            out.append(j)
            busy[j.owner] = busy.get(j.owner, 0) + 1
        return out


def settle(arena: Arena):
    """Moves submissions whose owed games are all played: draft -> done, placing -> rated."""
    with arena.lock:
        taken = {(g.a, g.b, g.suite, g.map, g.seed) for g in arena.games if current_game(g)}
        running = set(arena.running)
        for s in arena.subs.values():
            if s.status not in (QUEUED, PLAYING, PLACING):
                continue
            keys = {j.key for j in owed_for(arena, s)}
            if keys & running:
                if s.status == QUEUED:
                    s.status = PLAYING
                    arena.save_sub(s)
                continue
            if keys <= taken:
                s.status = DONE if s.lane == "draft" else RATED
                arena.save_sub(s)
