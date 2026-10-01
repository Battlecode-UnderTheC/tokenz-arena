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


class Arena:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.subs: dict[str, Submission] = {}
        self.games: list[Game] = []
        self.bots: dict[str, dict] = {}
        self.running: dict[tuple, dict] = {}     # job key -> {job, started}
        self.paused = False
        self.started = now()
        self._load()

    # ----------------------------------------------------------------- persistence
    def _load(self):
        for meta in sorted(self.cfg.submissions.glob("*/meta.json")):
            d = store.read_json(meta)
            self.subs[d["id"]] = Submission(**d)
        for g in store.load_all(self.cfg.games, "*.json"):
            self.games.append(Game(**g))
        self.games.sort(key=lambda g: g.id)
        self.bots = store.read_json(self.cfg.data_dir / "bots.json", {})
        # A submission caught mid-check or mid-play by a restart just continues.
        for s in self.subs.values():
            if s.status == PLAYING:
                s.status = QUEUED

    def save_sub(self, s: Submission):
        store.write_json(self.cfg.submissions / s.id / "meta.json", asdict(s))

    def save_bots(self):
        store.write_json(self.cfg.data_dir / "bots.json", self.bots)

    def add_game(self, g: Game) -> Game:
        with self.lock:
            g.id = (self.games[-1].id + 1) if self.games else 1
            self.games.append(g)
            store.write_json(self.cfg.games / f"{g.id}.json", asdict(g))
            return g

    def save_game(self, g: Game):
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

    def games_of(self, sid: str) -> list[Game]:
        return [g for g in self.games if sid in (g.a, g.b)]

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


RATED_LANES = {"draft", "placement", "continuous"}
HIDDEN_SUITES = {"holdout", "check"}     # never in ratings, listings or replays


def is_rated(g: Game) -> bool:
    """A local game between two submissions on a public suite (official, tournament, server,
    rotating): what ratings, records and head-to-heads count. Real server games (lane
    'server'), checks and the hidden holdout don't count."""
    return g.lane in RATED_LANES and g.suite not in HIDDEN_SUITES and not g.error and g.a != g.b


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
