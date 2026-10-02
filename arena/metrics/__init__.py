"""In-game metrics from replays (docs/METRICS.md): how each team stands as a game goes on.

A metric is a function in a module under arena/metrics/plugins/, registered with a decorator:

    from arena.metrics import metric

    @metric("dragons", "Dragons alive", "dragons the team has alive", version=1)
    def dragons(ctx, team):
        return len(ctx.ids(team))

It's called for each team every `EVERY` rounds (and at the game's last round) with a Ctx: the
replayed state at that round plus helpers. Derived metrics combine other metrics' values at a
round into one number for the game (e.g. the eval bar's P(A wins)):

    @derived("eval", "Eval", "P(team A wins), from the metrics at this round", version=1)
    def eval_bar(values, rnd):          # values: {metric: {"A": x, "B": y}}
        ...

compute(replay bytes) returns everything as one small JSON-able dict. Changing a metric means
bumping its version: the arena recomputes it for older games in the background.
"""

from __future__ import annotations

import collections
import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Callable

EVERY = 10
FORMAT = 2               # 2: adds "queens" (how and when each queen died)


@dataclass
class Metric:
    name: str
    title: str
    desc: str
    fn: Callable
    version: int = 1
    unit: str = ""
    better: str = "higher"          # higher | lower | none (for colouring and summaries)
    derived: bool = False
    fitted: str | None = None       # a fitted metric: the newest game its fit saw (ISO time)


REGISTRY: dict[str, Metric] = {}


def metric(name: str, title: str, desc: str = "", version: int = 1, unit: str = "",
           better: str = "higher"):
    def deco(fn):
        REGISTRY[name] = Metric(name, title, desc, fn, version, unit, better)
        return fn
    return deco


def derived(name: str, title: str, desc: str = "", version: int = 1, unit: str = "",
            better: str = "none", fitted: str | None = None):
    """`fitted`: for a metric fitted on games, the newest game the fit saw (ISO time), so the
    evals judge it only on later games (out of sample)."""
    def deco(fn):
        REGISTRY[name] = Metric(name, title, desc, fn, version, unit, better, derived=True,
                                fitted=fitted)
        return fn
    return deco


_loaded = False


def load_plugins():
    """Imports every module in arena/metrics/plugins (each registers its metrics)."""
    global _loaded
    if _loaded:
        return REGISTRY
    from arena.metrics import plugins
    for m in sorted(pkgutil.iter_modules(plugins.__path__), key=lambda m: m.name):
        importlib.import_module(f"arena.metrics.plugins.{m.name}")
    _loaded = True
    return REGISTRY


def versions() -> dict[str, int]:
    return {name: m.version for name, m in load_plugins().items()}


def describe() -> list[dict]:
    return [{"name": m.name, "title": m.title, "desc": m.desc, "version": m.version,
             "unit": m.unit, "better": m.better, "kind": "game" if m.derived else "team",
             "fitted": m.fitted}
            for m in load_plugins().values()]


# ---------------------------------------------------------------------------- the context

class Ctx:
    """What a metric sees at one round: the replayed state, and counters kept since the start.

    st      arena.metrics.state.State: st.body[id] (deque of tiles, head first), st.team[id],
            st.alive (ids), st.pearls (tiles), st.round
    g       the map: g.W, g.H, g.step(tile, dir), g.name
    rnd     rounds played so far
    cum     {team: Counter}: eat, deaths, deaths_enemy, deaths_self, lost, splits, kills, paid
    past    {team: [Counter after round 1, after round 2, ...]}: the counters round by round,
            for rates (ctx.rate)
    queens  {team: dragon id of its queen}
    cache   a dict shared by all metrics at this round (for expensive things like BFS)
    """

    def __init__(self, st, g, rnd, cum, queens, past=None):
        self.st, self.g, self.rnd, self.cum, self.queens = st, g, rnd, cum, queens
        self.past = past or {"A": [], "B": []}
        self.cache: dict = {}

    @staticmethod
    def other(team: str) -> str:
        return "B" if team == "A" else "A"

    def ids(self, team: str) -> list[int]:
        return [i for i in self.st.alive if self.st.team[i] == team]

    def lengths(self, team: str) -> list[int]:
        return sorted((len(self.st.body[i]) for i in self.ids(team)), reverse=True)

    def heads(self, team: str) -> list:
        return [self.st.body[i][0] for i in self.ids(team)]

    def queen(self, team: str) -> int | None:
        q = self.queens.get(team)
        return q if q is not None and q in self.st.alive else None

    def occupied(self) -> set:
        if "occupied" not in self.cache:
            occ = set()
            for i in self.st.alive:
                occ.update(self.st.body[i])
            self.cache["occupied"] = occ
        return self.cache["occupied"]

    def bfs(self, sources, blocked=frozenset(), limit=10 ** 9) -> dict:
        """Steps from any of `sources` to every reachable tile (kelp and portals included)."""
        g, dist, frontier, d = self.g, {}, [], 0
        for s in sources:
            if s not in dist:
                dist[s] = 0
                frontier.append(s)
        step = g._step
        while frontier and d < limit:
            d += 1
            nxt = []
            for t in frontier:
                for k in "NESW":
                    n = step[(t, k)]
                    if n is not None and n not in dist and n not in blocked:
                        dist[n] = d
                        nxt.append(n)
            frontier = nxt
        return dist

    def rate(self, team: str, key: str, window: int) -> float:
        """How much counter `key` grew per round over the last `window` rounds (fewer early on)."""
        hist = self.past[team]
        w = min(window, len(hist))
        if w <= 0:
            return 0.0
        before = hist[-w - 1][key] if len(hist) > w else 0
        return (self.cum[team][key] - before) / w

    def head_dist(self, team: str) -> dict:
        """Cached BFS distance from the team's heads (ignoring bodies)."""
        key = ("head_dist", team)
        if key not in self.cache:
            self.cache[key] = self.bfs(self.heads(team))
        return self.cache[key]


# ---------------------------------------------------------------------------- computing

QUEEN_CAUSES = ["hit a wall", "ran into itself", "ran into an enemy", "ran into an ally",
                "head-on: an enemy moved into it", "head-on: lunged by an enemy",
                "head-on: moved into an enemy", "head-on: moved into an ally", "head-on: an ally moved into it",
                "no valid move", "other", "not recorded yet"]


def queen_cause(reason: str, detail: str) -> str:
    """A queen's death in words (the decoder's reason and detail; a mover's head-on is settled
    at the end of its turn, by whether it took an enemy with it)."""
    if reason == "wall":
        return "hit a wall"
    if reason == "self":
        return "ran into itself"
    if reason == "other_body":
        return "ran into an ally" if detail == "into_ally" else "ran into an enemy"
    if reason == "head_to_head":
        if detail == "mover":
            return "head-on"                       # settled when its turn ends
        if detail.startswith("hit_by_enemy"):
            return "head-on: lunged by an enemy" if detail.endswith("_lunge") else "head-on: an enemy moved into it"
        return "head-on: an ally moved into it"
    if reason == "no_valid_action":
        return "no valid move"
    return "other"


def compute(blob: bytes, every: int = EVERY) -> dict:
    """All registered metrics for one replay (plain or gzip bytes)."""
    from arena.metrics import state as S
    reg = load_plugins()
    team_metrics = [m for m in reg.values() if not m.derived]
    game_metrics = [m for m in reg.values() if m.derived]
    d = S.load_bytes(blob)
    g = S.GameMap(d["map"])
    queens = {t: i for i, (t, _) in enumerate(g.dragons[:2])}
    # how each queen died: {team: {"round", "cause", "length"}} (None if it lived)
    queen_death: dict = {"A": None, "B": None}
    queen_ids = {i: t for t, i in queens.items()}
    cum = {t: collections.Counter() for t in "AB"}
    past = {t: [] for t in "AB"}
    rounds: list[int] = []
    team_series = {m.name: {"A": [], "B": []} for m in team_metrics}
    game_series = {m.name: [] for m in game_metrics}
    errors: dict[str, str] = {}

    def snapshot(st, rnd):
        ctx = Ctx(st, g, rnd, cum, queens, past)
        values = {}
        for m in team_metrics:
            v = {}
            for t in "AB":
                try:
                    x = m.fn(ctx, t)
                except Exception as e:                     # one bad metric doesn't stop the rest
                    errors.setdefault(m.name, f"{type(e).__name__}: {e}")
                    x = None
                v[t] = round(x, 4) if isinstance(x, float) else x
                team_series[m.name][t].append(v[t])
            values[m.name] = v
        for m in game_metrics:
            try:
                x = m.fn(values, rnd)
            except Exception as e:
                errors.setdefault(m.name, f"{type(e).__name__}: {e}")
                x = None
            game_series[m.name].append(round(x, 4) if isinstance(x, float) else x)
        rounds.append(rnd)

    last_state, last_done = None, 0
    for kind, st, info in S.walk(d):
        if kind == "turn":
            t = info["team"]
            cum[t]["eat"] += info["ate"]
            act = info["action"]
            if act and act[0] == "move":
                free = -(-info["len_before"] // 4)
                cum[t]["paid"] += max(0, len(act[1]) - free)
            if info["died"] == "head_to_head":
                # the mover's own head-to-head death: enemy-caused if it took an enemy with it
                enemy = any(st.team.get(k) != t for k in info["killed"])
                if enemy:
                    cum[t]["deaths_enemy"] += 1
                    cum[Ctx.other(t)]["kills"] += 1
                else:
                    cum[t]["deaths_self"] += 1
                qt = queen_ids.get(info["id"])
                if qt and queen_death[qt] and queen_death[qt]["cause"] == "head-on":
                    queen_death[qt]["cause"] = "head-on: moved into an enemy" if enemy else "head-on: moved into an ally"
        elif kind == "death":
            _, i, t, reason, length, detail = info
            if i in queen_ids:
                queen_death[queen_ids[i]] = {"round": info[0] + 1, "cause": queen_cause(reason, detail),
                                             "length": length}
            cum[t]["deaths"] += 1
            cum[t]["lost"] += length
            if reason == "head_to_head" and detail == "mover":
                continue                                   # classified at the end of its turn
            enemy = (reason == "head_to_head" and str(detail).startswith("hit_by_enemy")) or \
                    (reason == "other_body" and detail == "into_enemy")
            cum[t]["deaths_enemy" if enemy else "deaths_self"] += 1
            if enemy:
                cum[Ctx.other(t)]["kills"] += 1
        elif kind == "split":
            cum[info[3]]["splits"] += 1
        elif kind == "round_end":
            rnd = info + 1
            last_state, last_done = st, rnd
            for t in "AB":
                past[t].append(collections.Counter(eat=cum[t]["eat"], deaths=cum[t]["deaths"],
                                                   kills=cum[t]["kills"], splits=cum[t]["splits"]))
            if rnd % every == 0 or rnd == 1:
                snapshot(st, rnd)
    if last_state is not None and (not rounds or rounds[-1] != last_done):
        snapshot(last_state, last_done)
    res = d.get("result") or {}
    return {"format": FORMAT, "versions": {m.name: m.version for m in reg.values()}, "queens": queen_death,
            "every": every, "rounds": rounds, "team": team_series, "game": game_series,
            "map": {"name": g.name, "W": g.W, "H": g.H},
            "result": {"winner": res.get("winner"), "end": res.get("endReason")},
            "errors": errors}


def outdated(stored: dict | None) -> bool:
    """True if a stored metrics file lacks a registered metric or has an older version."""
    if not stored or stored.get("format") != FORMAT:
        return True
    have = stored.get("versions") or {}
    return any(have.get(k) != v for k, v in versions().items())
