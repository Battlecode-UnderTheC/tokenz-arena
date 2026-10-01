"""Bradley-Terry ratings over games among current ranked submissions (DESIGN.md section 4).

A draw counts as half a win. Each pair also gets one virtual draw, which keeps unbeaten or
winless submissions finite. Refit in full after each game (it's small), on an Elo scale:
the reference bot is fixed at 1000 when it's rated, otherwise the mean is 1500. `err` is a
95% interval from the fit's Fisher information.
"""

from __future__ import annotations

import math

from arena.state import Arena, is_rated

SCALE = 400 / math.log(10)


def fit(arena: Arena) -> dict[str, dict]:
    with arena.lock:
        current = [s.id for s in arena.current()]
        ids = set(current)
        games = [g for g in arena.games
                 if is_rated(g) and g.a in ids and g.b in ids]
        ref = arena.bots.get(arena.cfg.get("play", "reference_bot") or "", {}).get("current")
    if not current:
        return {}
    idx = {sid: i for i, sid in enumerate(current)}
    n = len(current)
    wins = [[0.0] * n for _ in range(n)]
    for g in games:
        i, j = idx[g.a], idx[g.b]
        if g.winner == "A":
            wins[i][j] += 1
        elif g.winner == "B":
            wins[j][i] += 1
        else:
            wins[i][j] += 0.5
            wins[j][i] += 0.5
    played = [[wins[i][j] + wins[j][i] for j in range(n)] for i in range(n)]
    for i in range(n):          # one virtual draw per pair that has met
        for j in range(n):
            if i != j and played[i][j]:
                wins[i][j] += 0.5
                played[i][j] += 1
    p = [1.0] * n
    for _ in range(500):
        new = []
        for i in range(n):
            w = sum(wins[i])
            d = sum(played[i][j] / (p[i] + p[j]) for j in range(n) if j != i and played[i][j])
            new.append(w / d if d and w else p[i] * 0.5 if d else p[i])
        g = math.exp(sum(math.log(x) for x in new) / n)
        new = [x / g for x in new]
        done = max(abs(math.log(a / b)) for a, b in zip(new, p)) < 1e-7
        p = new
        if done:
            break
    elo = [SCALE * math.log(x) for x in p]
    shift = 1000 - elo[idx[ref]] if ref in idx else 1500 - sum(elo) / n
    out = {}
    for sid, i in idx.items():
        info = sum(played[i][j] * p[i] * p[j] / (p[i] + p[j]) ** 2 for j in range(n) if j != i)
        err = 1.96 * SCALE / math.sqrt(info) if info else None
        real = sum(played[i][j] for j in range(n)) - sum(1 for j in range(n) if j != i and played[i][j])
        out[sid] = {"elo": round(elo[i] + shift, 1), "err": round(err, 1) if err else None,
                    "games": int(real)}
    return out
