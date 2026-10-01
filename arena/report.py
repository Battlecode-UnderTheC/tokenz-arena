"""A submission's diagnostic report (DESIGN.md section 5): by opponent, by map (worst first),
how its dragons die, CPU headroom against the 100M cap, child vs parent, worst losses."""

from __future__ import annotations

import math

from arena.state import Arena, current_engine, is_rated, result_for, wdl

CPU_CAP = 100_000_000


def _pct(r: dict) -> float | None:
    n = r["W"] + r["D"] + r["L"]
    return round(100 * (r["W"] + r["D"] / 2) / n, 1) if n else None


def interval(r: dict) -> dict:
    """95% Wilson interval on the score (a draw is half a win), and whether it excludes 50%.
    'noise' means the result is consistent with two equal bots: don't read it as a gain."""
    n = r["W"] + r["D"] + r["L"]
    if not n:
        return {"ci": None, "significant": None, "games": 0}
    p, z = (r["W"] + r["D"] / 2) / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    lo, hi = round(100 * (c - h), 1), round(100 * (c + h), 1)
    return {"ci": [lo, hi], "significant": lo > 50 or hi < 50, "games": n}


def _row(r: dict) -> dict:
    return {**r, "score": _pct(r), **interval(r)}


def _beats(r: dict) -> bool | None:
    """True if it won more than it lost against its parent, False if fewer, None if even."""
    return True if r["W"] > r["L"] else False if r["W"] < r["L"] else None


def build(arena: Arena, sid: str) -> dict:
    with arena.lock:
        s = arena.subs[sid]
        games = [g for g in arena.games_of(sid) if g.a != g.b]
        local = [g for g in games if is_rated(g)]
        holdout = [g for g in games if g.suite == "holdout" and current_engine(g) and not g.error]
        historical = sum(1 for g in games if not current_engine(g))
    by_opp: dict[str, list] = {}
    by_map: dict[str, list] = {}
    deaths: dict[str, int] = {}
    errors, points_p99, points_max, rounds = 0, [], [], []
    losses = []
    for g in local:
        opp = g.b if g.a == sid else g.a
        by_opp.setdefault(opp, []).append(g)
        by_map.setdefault(f"{g.suite}/{g.map}", []).append(g)
        seat = "A" if g.a == sid else "B"
        t = (g.stats.get("teams") or {}).get(seat, {})
        for cause, k in (t.get("deaths") or {}).items():
            deaths[cause] = deaths.get(cause, 0) + k
        errors += t.get("errors", 0) + (1 if g.error else 0)
        if t.get("points"):
            points_p99.append(t["points"].get("p99", 0))
            points_max.append(t["points"].get("max", 0))
        if g.rounds:
            rounds.append(g.rounds)
        if result_for(g, sid) == "L":
            margin = (g.stats.get("tiebreak") or {})
            losses.append((0 if g.rounds and g.rounds < 500 else 1,
                           -(margin.get("winner", 0) - margin.get("loser", 0)), g))
    by_seat = {"A": wdl([g for g in local if g.a == sid], sid),
               "B": wdl([g for g in local if g.b == sid], sid)}
    by_end: dict[str, dict] = {}
    econ: dict[int, list] = {50: [], 100: []}
    for g in local:
        end = "draw" if g.winner is None else (g.reason or "?").split(",")[0]
        row = by_end.setdefault(end, {"W": 0, "D": 0, "L": 0})
        row[result_for(g, sid) or "D"] += 1
        mine, theirs = (1, 2) if g.a == sid else (2, 1)
        for point in (g.stats.get("dragons") or []):
            if point[0] in econ:
                econ[point[0]].append((point[mine], point[theirs]))
    economy = {f"r{k}": ({"dragons": round(sum(a for a, _ in v) / len(v), 1),
                          "opponent_dragons": round(sum(b for _, b in v) / len(v), 1),
                          "games": len(v)} if v else None) for k, v in econ.items()}
    opp_rows = []
    for opp, gs in by_opp.items():
        r = wdl(gs, sid)
        opp_rows.append({"opponent": opp, **_row(r)})
    map_rows = []
    for m, gs in by_map.items():
        r = wdl(gs, sid)
        map_rows.append({"map": m, **_row(r)})
    losses.sort(key=lambda x: (x[0], x[1]))
    total = wdl(local, sid)
    rep = {
        "submission": sid, "status": s.status, "lane": s.lane, "games": len(local),
        "record": _row(total),
        "by_seat": {k: _row(v) for k, v in by_seat.items()},
        "by_end": {k: _row(v) for k, v in sorted(by_end.items(), key=lambda x: -sum(x[1].values()))},
        "economy": economy,
        "historical_games": historical,
        "by_opponent": sorted(opp_rows, key=lambda r: (r["score"] is None, r["score"] or 0)),
        "by_map": sorted(map_rows, key=lambda r: (r["score"] is None, r["score"] or 0)),
        "deaths": dict(sorted(deaths.items(), key=lambda x: -x[1])),
        "deaths_per_game": round(sum(deaths.values()) / len(local), 2) if local else None,
        "errors": errors,
        "cpu": {"p99_max": max(points_p99, default=None), "max": max(points_max, default=None),
                "cap": CPU_CAP,
                "headroom": (round(1 - max(points_max) / CPU_CAP, 3) if points_max else None)},
        "rounds_mean": round(sum(rounds) / len(rounds), 1) if rounds else None,
        "worst_losses": [{"game": g.id, "map": g.map, "opponent": g.b if g.a == sid else g.a,
                          "rounds": g.rounds, "reason": g.reason,
                          "replay": f"/api/v1/games/{g.id}/replay" if g.replay else None}
                         for _, _, g in losses[:5]],
        "holdout": wdl(holdout, sid) if holdout else None,
    }
    if s.parent:
        vs = [g for g in local if s.parent in (g.a, g.b)]
        r = wdl(vs, sid)
        rep["vs_parent"] = {"parent": s.parent, **_row(r),
                            "beats_parent": _beats(r) if vs else None}
    return rep


def verdict(arena: Arena, sid: str) -> dict:
    """The short form used for drafts and /ideas: vs parent and vs each frozen opponent."""
    with arena.lock:
        s = arena.subs[sid]
        games = [g for g in arena.games_of(sid) if g.lane in ("draft", "placement") and is_rated(g)]
    rows = []
    for opp in s.opponents or []:
        r = wdl([g for g in games if opp in (g.a, g.b)], sid)
        rows.append({"opponent": opp, **_row(r)})
    out = {"complete": s.status in ("done", "rated"), "opponents": rows}
    if s.parent:
        r = wdl([g for g in games if s.parent in (g.a, g.b)], sid)
        n = r["W"] + r["D"] + r["L"]
        out["vs_parent"] = {"parent": s.parent, **_row(r),
                            "beats_parent": _beats(r) if n else None}
        vp = out["vs_parent"]
        vp["summary"] = (None if not n else
                         f"{'beats' if vp['beats_parent'] else 'loses to' if vp['beats_parent'] is False else 'even with'}"
                         f" parent, {vp['score']}% (95% CI {vp['ci'][0]}-{vp['ci'][1]}) over {n} games"
                         + ("" if vp["significant"] else ": within noise"))
    return out
