"""Reading stored metrics for the API and website: one game's series, a submission's summary,
and the metric value at a round."""

from __future__ import annotations

from arena import store

SUMMARY_ROUNDS = (50, 100, 200, 300, 400)


def game(arena, gid: int) -> dict | None:
    return store.read_json(arena.cfg.data_dir / "metrics" / f"{gid}.json", None)


def at(m: dict, rnd: int) -> int | None:
    """Index of the last snapshot at or before `rnd` (None if none)."""
    idx = None
    for i, r in enumerate(m.get("rounds") or []):
        if r <= rnd:
            idx = i
    return idx


def submission(arena, sid: str, limit: int = 300) -> dict:
    """Mean of each team metric for this submission's side vs its opponents, at a few rounds and
    at the end, over its most recent games with metrics; plus the mean eval from its side."""
    from arena import metrics as M
    reg = {d["name"]: d for d in M.describe()}
    with arena.lock:
        games = [g for g in reversed(arena.games) if sid in (g.a, g.b) and g.a != g.b
                 and g.suite not in ("holdout", "check")][:limit]
    sums: dict = {}
    n_games = 0
    for g in games:
        m = game(arena, g.id)
        if not m or m.get("error"):
            continue
        n_games += 1
        me = "A" if g.a == sid else "B"
        them = "B" if me == "A" else "A"
        points = [(f"r{r}", at(m, r)) for r in SUMMARY_ROUNDS if m["rounds"] and m["rounds"][-1] >= r]
        points.append(("end", len(m["rounds"]) - 1 if m["rounds"] else None))
        for name, s in (m.get("team") or {}).items():
            for label, i in points:
                if i is None:
                    continue
                a, b = s[me][i], s[them][i]
                if a is None or b is None:
                    continue
                cell = sums.setdefault(name, {}).setdefault(label, [0.0, 0.0, 0])
                cell[0] += a
                cell[1] += b
                cell[2] += 1
        ev = (m.get("game") or {}).get("eval")
        if ev:
            for label, i in points:
                if i is None or ev[i] is None:
                    continue
                cell = sums.setdefault("eval", {}).setdefault(label, [0.0, 0.0, 0])
                p = ev[i] if me == "A" else 1 - ev[i]
                cell[0] += p
                cell[1] += 1 - p
                cell[2] += 1
    out = {}
    for name, by in sums.items():
        out[name] = {"title": reg.get(name, {}).get("title", name), "better": reg.get(name, {}).get("better"),
                     "at": {k: {"own": round(v[0] / v[2], 3), "opponents": round(v[1] / v[2], 3), "games": v[2]}
                            for k, v in by.items()}}
    return {"submission": sid, "games": n_games, "metrics": out}
