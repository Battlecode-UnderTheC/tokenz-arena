"""Two bots side by side, from their games' in-game metrics (the /compare page and
/api/v1/compare): per map, round by round, how their queens die, and early vs mid vs late game.

Every number is from the bot's own seat (its metric, its opponent's, its win chance), averaged
over the games it played: all its rated games, or only the two bots' games against each other
(scope "h2h"), on every map or one. A game counts while it lasts: the round-by-round averages at
round r are over the games still running at r.
"""

from __future__ import annotations

import collections
import statistics
import threading

from arena.metrics import FORMAT, QUEEN_CAUSES
from arena.state import Arena, is_rated

ROUNDS = list(range(10, 501, 10))
PHASES = (("early", 1, 150), ("mid", 151, 350), ("late", 351, 500))
CURVES = ["eval", "dragons", "total_length", "longest", "board_control", "pearl_rate", "queen_length",
          "queen_room", "queen_danger"]
_CACHE: dict = {}
_LOCK = threading.Lock()


def _games(arena: Arena, sid: str, versus: str | None) -> list:
    with arena.lock:
        gs = [g for g in arena.games_of(sid) if is_rated(g) and g.a != g.b]
    if versus:
        gs = [g for g in gs if versus in (g.a, g.b)]
    return gs


def _ending(winner_is_me: bool | None, end: str | None, mine: dict, theirs: dict) -> str:
    """How the game ended, from this bot's side."""
    if end != "roundLimit":
        return "won by elimination" if winner_is_me else ("lost by elimination" if winner_is_me is False
                                                           else "both eliminated")
    word = "won" if winner_is_me else "lost" if winner_is_me is False else "drew"
    if winner_is_me is None:
        return "drew at 500"
    for k, label in (("queen_length", "queen"), ("longest", "longest"), ("total_length", "total")):
        if mine.get(k) != theirs.get(k):
            return f"{word} at 500 on {label}"
    return f"{word} at 500"


def per_game(arena: Arena, sid: str, g) -> dict | None:
    """One game from `sid`'s seat: its series every 10 rounds, the opponent's, the end, its queen."""
    from arena import store
    m = store.read_json(arena.cfg.data_dir / "metrics" / f"{g.id}.json", None)
    if not m or m.get("error") or not m.get("rounds") or (m.get("format") or 1) > FORMAT:
        return None
    me, them = ("A", "B") if g.a == sid else ("B", "A")
    idx = {r: i for i, r in enumerate(m["rounds"])}
    series = {}
    for k in CURVES:
        if k == "eval":
            src = m["game"].get("eval")
            if src is None:
                continue
            vals = [None if v is None else (v if me == "A" else 1 - v) for v in src]
            series[k] = ({r: vals[i] for r, i in idx.items()}, None)
            continue
        t = m["team"].get(k)
        if not t:
            continue
        series[k] = ({r: t[me][i] for r, i in idx.items()}, {r: t[them][i] for r, i in idx.items()})
    last = m["rounds"][-1]
    final_me = {k: (m["team"][k][me][-1] if k in m["team"] else None) for k in ("queen_length", "longest", "total_length")}
    final_them = {k: (m["team"][k][them][-1] if k in m["team"] else None) for k in ("queen_length", "longest", "total_length")}
    winner = m["result"]["winner"]
    won = None if winner not in ("A", "B") else winner == me
    if "queens" in m:
        q, qo = m["queens"].get(me), m["queens"].get(them)
    else:                                  # an older file, not recomputed yet: when, but not how
        q, qo = (_queen_from_snapshots(m, t) for t in (me, them))
    return {"id": g.id, "map": g.map, "opponent": g.b if me == "A" else g.a, "last": last, "won": won,
            "ending": _ending(won, m["result"]["end"], final_me, final_them), "series": series,
            "queen": q, "their_queen": qo}


def _queen_from_snapshots(m: dict, team: str) -> dict | None:
    """A queen's death from the every-10-rounds snapshots (files from before queen deaths were
    recorded): the first snapshot it's gone, cause unknown."""
    alive = (m["team"].get("queen_alive") or {}).get(team)
    if not alive:
        return None
    for r, v in zip(m["rounds"], alive):
        if not v:
            return {"round": r, "cause": "not recorded yet", "length": None}
    return None


def _collect(arena: Arena, sid: str) -> dict[int, dict]:
    """Every game of `sid` with metrics, from its seat (cached until it has new games)."""
    games = _games(arena, sid, None)
    key = (sid, games[-1].id if games else 0, len(games))
    with _LOCK:
        hit = _CACHE.get(sid)
        if hit and hit[0] == key:
            return hit[1]
    out = {}
    for g in games:
        d = per_game(arena, sid, g)
        if d:
            out[g.id] = d
    with _LOCK:
        _CACHE[sid] = (key, out)
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 4) if xs else None


def summarise(games: list[dict]) -> dict:
    """The comparison for one bot over these games."""
    n = len(games)
    out: dict = {"games": n}
    if not n:
        return out
    wins = sum(1 for d in games if d["won"])
    draws = sum(1 for d in games if d["won"] is None)
    out["score"] = round(100 * (wins + draws / 2) / n, 1)
    out["record"] = {"W": wins, "D": draws, "L": n - wins - draws}
    out["avg_rounds"] = round(statistics.fmean(d["last"] for d in games), 1)
    out["endings"] = dict(collections.Counter(d["ending"] for d in games).most_common())
    # round by round: means over the games still running
    curves = {}
    for k in CURVES:
        mine, theirs, alive = [], [], []
        for r in ROUNDS:
            running = [d for d in games if d["last"] >= r and k in d["series"]]
            alive.append(len(running))
            mine.append(_mean([d["series"][k][0].get(r) for d in running]))
            theirs.append(_mean([d["series"][k][1].get(r) for d in running]) if k != "eval" else None)
        curves[k] = {"mine": mine, "theirs": theirs if k != "eval" else None}
    out["curves"] = curves
    out["running"] = [sum(1 for d in games if d["last"] >= r) for r in ROUNDS]
    # queens
    deaths = [d["queen"] for d in games if d["queen"]]
    survival = []
    for r in ROUNDS:
        at_risk = [d for d in games if d["last"] >= r or d["queen"]]
        alive = sum(1 for d in at_risk if not d["queen"] or d["queen"]["round"] > r)
        survival.append(round(100 * alive / len(at_risk), 1) if at_risk else None)
    phase_deaths = {name: sum(1 for q in deaths if lo <= q["round"] <= hi) for name, lo, hi in PHASES}
    causes = collections.Counter(q["cause"] for q in deaths)
    out["queen"] = {
        "died": len(deaths), "died_pct": round(100 * len(deaths) / n, 1),
        "median_death_round": statistics.median(q["round"] for q in deaths) if deaths else None,
        "survival": survival,
        "causes": {c: causes[c] for c in QUEEN_CAUSES if causes.get(c)},
        "phases": phase_deaths,
        "won_when_queen_died": round(100 * sum(1 for d in games if d["queen"] and d["won"]) / len(deaths), 1) if deaths else None,
        "won_when_queen_lived": (round(100 * sum(1 for d in games if not d["queen"] and d["won"])
                                       / sum(1 for d in games if not d["queen"]), 1)
                                 if any(not d["queen"] for d in games) else None),
        "killed_their_queen": sum(1 for d in games if d["their_queen"]),
    }
    # early / mid / late: phase means of each metric (over the rounds the game reached in it)
    phases = {}
    for name, lo, hi in PHASES:
        rs = [r for r in ROUNDS if lo <= r <= hi]
        row = {}
        for k in CURVES:
            vals, opp = [], []
            for d in games:
                if k not in d["series"]:
                    continue
                s_me, s_them = d["series"][k]
                vals += [s_me.get(r) for r in rs if r <= d["last"]]
                if s_them:
                    opp += [s_them.get(r) for r in rs if r <= d["last"]]
            row[k] = {"mine": _mean(vals), "theirs": _mean(opp) if k != "eval" else None}
        ahead = [d["series"]["eval"][0].get(r) for d in games if "eval" in d["series"] for r in rs if r <= d["last"]]
        ahead = [x for x in ahead if x is not None]
        row["ahead_pct"] = round(100 * sum(1 for x in ahead if x > 0.5) / len(ahead), 1) if ahead else None
        row["games_reaching"] = sum(1 for d in games if d["last"] >= lo)
        phases[name] = row
    out["phases"] = phases
    return out


def compare(arena: Arena, a: str, b: str, map: str | None = None, scope: str = "all") -> dict:
    """Bots a and b side by side. scope "all": each over all its games; "h2h": only a vs b."""
    out = {"a": a, "b": b, "map": map, "scope": scope, "rounds": ROUNDS, "phases": [p[0] for p in PHASES],
           "causes": QUEEN_CAUSES, "curves": CURVES}
    sides = {}
    for me, other in ((a, b), (b, a)):
        games = list(_collect(arena, me).values())
        if scope == "h2h":
            games = [d for d in games if d["opponent"] == other]
        maps = collections.Counter(d["map"] for d in games)
        if map:
            games = [d for d in games if d["map"] == map]
        s = summarise(games)
        s["maps"] = dict(maps.most_common())
        sides[me] = s
    out["sides"] = sides
    # per map: both bots' score, length and queen survival
    all_maps = sorted(set(sides[a]["maps"]) | set(sides[b]["maps"]))
    rows = []
    for mp in all_maps:
        row = {"map": mp}
        for me, other in ((a, b), (b, a)):
            games = [d for d in _collect(arena, me).values() if d["map"] == mp
                     and (scope != "h2h" or d["opponent"] == other)]
            n = len(games)
            row[me] = None if not n else {
                "games": n,
                "score": round(100 * (sum(1 for d in games if d["won"]) + sum(1 for d in games if d["won"] is None) / 2) / n, 1),
                "avg_rounds": round(statistics.fmean(d["last"] for d in games), 1),
                "queen_died_pct": round(100 * sum(1 for d in games if d["queen"]) / n, 1),
                "top_ending": collections.Counter(d["ending"] for d in games).most_common(1)[0][0]}
        rows.append(row)
    out["per_map"] = rows
    return out
