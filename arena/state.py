"""The arena's state: submissions, bots and games, held in memory and persisted as JSON
files (store.py). Every mutation goes through Arena under its lock and is written at once.

    submissions/<id>/meta.json   Submission
    submissions/<id>/source.zip  the upload
    games/<id>.json              Game
    bots.json                    {bot: {owner, active, current}}
    server/                      the server bridge's own files (arena/server/)
"""

from __future__ import annotations

import threading
import time
from importlib.metadata import PackageNotFoundError, version
from dataclasses import asdict, dataclass, field
from pathlib import Path

from arena import store
from arena.config import Config

# Submission statuses
CHECKING, REJECTED, QUEUED, PLAYING, DONE, PLACING, RATED, RETIRED = (
    "checking", "rejected", "queued", "playing", "done", "placing", "rated", "retired")
EXTERNAL = "external"   # a server opponent (server:<team>): never scheduled locally


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Submission:
    id: str                       # <bot>-<n>
    bot: str
    owner: str
    lane: str                     # draft | ranked
    status: str
    sha256: str = ""
    agent: str | None = None
    parent: str | None = None
    note: str = ""
    source: str | None = None     # a GitHub URL: the bot's directory at a commit
    language: str | None = None
    reason: str | None = None     # why rejected
    check_log: str | None = None
    created: str = field(default_factory=now)
    promoted: str | None = None
    promoted_by: str | None = None
    warnings: list = field(default_factory=list)   # lints that don't reject (e.g. map names)
    note_history: list = field(default_factory=list)   # earlier notes: [{at, by, note}]
    parent_history: list = field(default_factory=list)  # earlier parents: [{at, by, parent}]
    quick_opponents: list | None = None   # placement's first round: parent + top bots
    engine: str = ""                      # the judge (rules) version it was submitted under
    retired_at: str | None = None
    retired_by: str | None = None
    opponents: list | None = None   # frozen when its draft or placement starts


@dataclass
class Game:
    id: int
    lane: str                     # draft | placement | holdout | continuous | check | server
    suite: str                    # official | rotating | holdout | server | check
    map: str
    seed: int
    a: str                        # submission id in seat A
    b: str
    winner: str | None = None     # A | B | None (draw, or error)
    rounds: int | None = None
    reason: str | None = None
    stats: dict = field(default_factory=dict)
    replay: str | None = None     # file name under replays/, None once pruned
    seconds: float | None = None
    error: str | None = None
    played: str = field(default_factory=now)
    meta: dict = field(default_factory=dict)   # e.g. server battle ids


class Subs(dict):
    """Submissions by id. Ids that were renamed still resolve: `aliases` maps an old id to its
    current one (data_dir/aliases.json), so links, notes and parents keep working."""

    def __init__(self):
        super().__init__()
        self.aliases: dict[str, str] = {}

    def _key(self, k):
        if k is None or dict.__contains__(self, k):
            return k
        return self.aliases.get(k, k)

    def get(self, k, default=None):
        return dict.get(self, self._key(k), default)

    def __getitem__(self, k):
        return dict.__getitem__(self, self._key(k))

    def __contains__(self, k):
        return dict.__contains__(self, self._key(k))


class Arena:
    """The arena's state. The engine (`arena run`) owns it and writes every change. The
    website (`arena web`) opens it with readonly=True: it never writes, and refresh() picks up
    the engine's changes from the files (called every second)."""

    def __init__(self, cfg: Config, readonly: bool = False):
        self.cfg = cfg
        self.readonly = readonly
        MAP_CFG[0] = cfg
        self._stamps: dict = {}
        self.lock = threading.RLock()
        self.subs: Subs = Subs()
        self.games: list[Game] = []
        self.bots: dict[str, dict] = {}
        self.running: dict[tuple, dict] = {}     # job key -> {job, started}
        self.by_sub: dict[str, list[Game]] = {}  # submission id -> its games (games_of)
        self.paused = False
        self.started = now()
        self._load()

    # ----------------------------------------------------------------- persistence
    def resolve(self, sid: str | None) -> str | None:
        """The current id of a submission id that may have been renamed."""
        return self.subs.aliases.get(sid, sid) if sid and not dict.__contains__(self.subs, sid) else sid

    def _load(self):
        self.subs.aliases = store.read_json(self.cfg.data_dir / "aliases.json", {}) or {}
        for meta in sorted(self.cfg.submissions.glob("*/meta.json")):
            d = store.read_json(meta)
            self.subs[d["id"]] = Submission(**d)
        for g in store.load_all(self.cfg.games, "*.json"):
            self.games.append(Game(**g))
        self.games.sort(key=lambda g: g.id)
        self._index()
        self.bots = store.read_json(self.cfg.data_dir / "bots.json", {})
        # A submission caught mid-check or mid-play by a restart just continues.
        for s in self.subs.values():
            if s.status == PLAYING:
                s.status = QUEUED

    def save_sub(self, s: Submission):
        if not self.readonly:
            store.write_json(self.cfg.submissions / s.id / "meta.json", asdict(s))

    def save_bots(self):
        if not self.readonly:
            store.write_json(self.cfg.data_dir / "bots.json", self.bots)

    def refresh(self, full: bool = False):
        """A read-only replica catching up with the engine's files: changed submissions (by
        mtime), new games (ids count up), bots, ratings and what's running (heartbeat).
        `full` also reloads every game, for rare in-place edits (re-labels, pruned replays)."""
        def changed(p: Path) -> bool:
            try:
                m = p.stat().st_mtime_ns
            except FileNotFoundError:
                return False
            if self._stamps.get(p) == m:
                return False
            self._stamps[p] = m
            return True
        subs = {}
        for meta in self.cfg.submissions.glob("*/meta.json"):
            if changed(meta):
                d = store.read_json(meta)
                if d:
                    subs[d["id"]] = Submission(**d)
        new_games = []
        if full:
            new_games = [Game(**g) for g in store.load_all(self.cfg.games, "*.json")]
        else:
            nxt = (self.games[-1].id + 1) if self.games else 1
            while (p := self.cfg.games / f"{nxt}.json").exists():
                d = store.read_json(p)
                if not d:
                    break
                new_games.append(Game(**d))
                nxt += 1
        bots = store.read_json(self.cfg.data_dir / "bots.json", None) \
            if changed(self.cfg.data_dir / "bots.json") else None
        ratings = store.read_json(self.cfg.data_dir / "ratings.json", None) \
            if changed(self.cfg.data_dir / "ratings.json") else None
        aliases = store.read_json(self.cfg.data_dir / "aliases.json", None) \
            if changed(self.cfg.data_dir / "aliases.json") else None
        hb = store.read_json(self.cfg.data_dir / "heartbeat.json", {}) or {}
        from arena.scheduler import LANES, Job       # (no import cycle at module load)
        prio = {v: k for k, v in LANES.items()}
        running = {}
        for r in hb.get("running") or []:
            if not isinstance(r, dict):
                continue
            j = Job(r.get("priority", prio.get(r.get("lane"), 3)), r["a"], r["b"], r["suite"],
                    r["map"], r["seed"], r.get("owner", ""), r.get("for_sub", r["a"]))
            running[j.key] = {"job": j, "started": r.get("started"), "worker": r.get("worker", "local")}
        with self.lock:
            if aliases is not None:
                self.subs.aliases = aliases
                for old in aliases:                   # renamed: drop the copy under its old id
                    if dict.__contains__(self.subs, old) and old not in subs:
                        dict.pop(self.subs, old, None)
            self.subs.update(subs)
            if full:
                self.games = sorted(new_games, key=lambda g: g.id)
                self._index()
            elif new_games:
                self.games.extend(new_games)
                for g in new_games:
                    self._index_game(g)
            if bots is not None:
                self.bots = bots
            if ratings is not None:
                self.ratings = ratings
            self.running = running
            self.heartbeat = hb

    def add_game(self, g: Game) -> Game:
        with self.lock:
            g.id = (self.games[-1].id + 1) if self.games else 1
            self.games.append(g)
            self._index_game(g)
            store.write_json(self.cfg.games / f"{g.id}.json", asdict(g))
            return g

    def save_game(self, g: Game):
        if not self.readonly:
            store.write_json(self.cfg.games / f"{g.id}.json", asdict(g))

    # ----------------------------------------------------------------- queries
    def next_id(self, bot: str) -> str:
        n = sum(1 for s in self.subs.values() if s.bot == bot) + 1
        while f"{bot}-{n}" in self.subs:
            n += 1
        return f"{bot}-{n}"

    def current(self) -> list[Submission]:
        """Each active bot's current ranked submission."""
        out = []
        for name, b in self.bots.items():
            s = self.subs.get(b.get("current") or "")
            if b.get("active", True) and s and s.status in (PLACING, RATED):
                out.append(s)
        return out

    def _index(self):
        self.by_sub = {}
        for g in self.games:
            self._index_game(g)

    def _index_game(self, g: Game):
        self.by_sub.setdefault(g.a, []).append(g)
        if g.b != g.a:
            self.by_sub.setdefault(g.b, []).append(g)

    def games_of(self, sid: str) -> list[Game]:
        """A submission's games, oldest first (from the index, not a scan)."""
        return list(self.by_sub.get(sid, ()))

    def by_sha(self, sha: str) -> Submission | None:
        return next((s for s in self.subs.values() if s.sha256 == sha), None)

    def ensure_external(self, team: str) -> Submission:
        """The stand-in for a competition-server opponent, so server games fit the model."""
        sid = f"server:{team}"
        with self.lock:
            if sid not in self.subs:
                s = Submission(id=sid, bot=sid, owner="server", lane="external", status=EXTERNAL,
                               sha256=sid, note=f"{team} on the competition server")
                self.subs[sid] = s
                self.save_sub(s)
            return self.subs[sid]


try:
    ENGINE = version("unswbc")            # the judge every local game here is played with
except PackageNotFoundError:
    ENGINE = "unknown"
FIRST_ENGINE = "1.2.3"                    # games recorded before games carried their engine
# Toolkit releases whose judge is byte-identical to an earlier one's (same rules, same results):
# their games stay comparable. 1.2.4-1.2.7 changed only the bundled maps and the replay viewer.
SAME_RULES = {"1.2.7": "1.2.3"}
MAP_CFG: list = [None]                    # the arena's config, for current_map (set by Arena)

RATED_LANES = {"draft", "placement", "continuous"}
HIDDEN_SUITES = {"holdout", "check"}     # never in ratings, listings or replays


def is_rated(g: Game) -> bool:
    """A local game between two submissions on a public suite (official, tournament, server,
    rotating): what ratings, records and head-to-heads count. Real server games (lane
    'server'), checks and the hidden holdout don't count."""
    return (g.lane in RATED_LANES and g.suite not in HIDDEN_SUITES and not g.error and g.a != g.b
            and current_game(g))


def rules(version: str) -> str:
    return SAME_RULES.get(version, version)


def current_engine(g: Game) -> bool:
    """Played under the judge running now (or one with the same rules). When the rules change,
    older games become historical: kept and shown, but out of ratings and verdicts."""
    return rules((g.meta or {}).get("engine", FIRST_ENGINE)) == rules(ENGINE)


def current_map(g: Game) -> bool:
    """Played on the map's current content (meta.map_sha). A map that changed since is history,
    like an old judge's games; a map no longer in play at all (last week's rotating set) keeps
    its games."""
    cfg = MAP_CFG[0]
    if cfg is None or g.suite == "check" or g.lane == "server":
        return True
    from arena import maps
    by_key, by_name = maps.live(cfg)
    sha = by_key.get((g.suite, g.map)) or by_name.get(maps.norm(g.map))
    mine = (g.meta or {}).get("map_sha")
    return sha is None or mine is None or mine == sha      # None: not stamped yet (migrate.py)


def current_game(g: Game) -> bool:
    return current_engine(g) and current_map(g)


def result_for(g: Game, sid: str) -> str | None:
    """'W', 'D' or 'L' for sid in game g (None if the game errored)."""
    if g.error:
        return None
    if g.winner is None:
        return "D"
    seat = "A" if g.a == sid else "B"
    return "W" if g.winner == seat else "L"


def wdl(games: list[Game], sid: str) -> dict:
    out = {"W": 0, "D": 0, "L": 0}
    for g in games:
        r = result_for(g, sid)
        if r:
            out[r] += 1
    return out
