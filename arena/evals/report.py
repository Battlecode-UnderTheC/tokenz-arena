"""How well the metrics predict real games: the evals report.

Each game is first reduced to a small digest (the eval and the leader under the round-500 rule
every 10 rounds, each team metric at a few rounds, how it ended), so the collector can keep
thousands in memory; build() turns digests into the report, per time window.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

EVAL_ROUNDS = tuple(range(10, 500, 10))
SHOW = (50, 100, 200, 300, 400, 490)
WINDOWS = (("24h", 86400), ("7d", 7 * 86400), ("all", None))
PHASES = (("early", 10, 150), ("mid", 160, 350), ("late", 360, 490))
ELO = (("1900+", 1900, 10 ** 6), ("1700-1899", 1700, 1900), ("under 1700", -10 ** 6, 1700))


def ts(at: str | None) -> float:
    try:
        return datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _leader(m: dict, i: int) -> int:
    for k in ("queen_length", "longest", "total_length"):
        s = m["team"].get(k)
        if not s:
            continue
        a, b = s["A"][i] or 0, s["B"][i] or 0
        if a != b:
            return 1 if a > b else -1
    return 0


def ending(d: dict) -> str:
    w, f = d["winner"], d["final"]
    if d["end"] != "roundLimit":
        return "elimination" if w in ("A", "B") else f"other ({d['end']})"
    if w not in ("A", "B"):
        return "draw at 500"
    qa, qb = f["queen_length"]
    if qa != qb:
        return "500: queen decides, one alive" if 0 in (qa, qb) else "500: queen decides, both alive"
    if qa == 0:
        return "500: both queens dead"
    return "500: queens equal"


def digest(info: dict, m: dict) -> dict | None:
    """The parts of one game the report needs; None if its metrics aren't usable."""
    if not m or m.get("error") or not m.get("rounds"):
        return None
    rounds = m["rounds"]
    last = rounds[-1]
    idx = {r: i for i, r in enumerate(rounds)}
    dr = m["team"].get("dragons") or {"A": [1] * len(rounds), "B": [1] * len(rounds)}
    ev, lead = {}, {}
    for r in EVAL_ROUNDS:
        i = idx.get(r)
        if i is None or r >= last or not dr["A"][i] or not dr["B"][i]:
            continue
        if m["game"].get("eval") and m["game"]["eval"][i] is not None:
            ev[r] = m["game"]["eval"][i]
        lead[r] = _leader(m, i)
    team = {}
    for name, s in m["team"].items():
        t = {str(r): (s["A"][idx[r]], s["B"][idx[r]]) for r in SHOW if r in idx and r < last}
        t["end"] = (s["A"][-1], s["B"][-1])
        team[name] = t
    final = {k: (m["team"][k]["A"][-1] or 0, m["team"][k]["B"][-1] or 0) if k in m["team"] else (0, 0)
             for k in ("queen_length", "longest", "total_length")}
    teams = info.get("teams") or {}
    elos = [t.get("elo") for t in teams.values() if t.get("elo")]
    return {"id": info["id"], "battle": info.get("battle"), "at": info.get("at"), "ts": ts(info.get("at")),
            "map": info.get("map"), "seats": info.get("seats") or {}, "ranked": info.get("ranked"),
            "elo": min(elos) if elos else None, "winner": m["result"]["winner"],
            "end": m["result"]["end"], "rounds": last, "versions": m.get("versions") or {},
            "eval": ev, "leader": lead, "team": team, "final": final}


# ---------------------------------------------------------------------------- statistics

def auc(scores: list[float], ys: list[bool]) -> float | None:
    """P(score of a random positive > a random negative), ties half (Mann-Whitney)."""
    pos = sum(ys)
    neg = len(ys) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    rank_sum, i = 0.0, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        rank_sum += sum(avg for k in range(i, j + 1) if ys[order[k]])
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def _r(x, d=4):
    return None if x is None else round(x, d)


def eval_rows(ds: list[dict]) -> list[dict]:
    rows = []
    for r in EVAL_ROUNDS:
        ps, ys, ls = [], [], []
        for d in ds:
            p = d["eval"].get(r) if isinstance(d["eval"], dict) else None
            if p is None:
                p = d["eval"].get(str(r))
            if p is None:
                continue
            ps.append(p)
            ys.append(d["winner"] == "A")
            ls.append(d["leader"].get(r, d["leader"].get(str(r), 0)))
        n = len(ps)
        if not n:
            rows.append({"round": r, "n": 0})
            continue
        clip = [min(max(p, 1e-4), 1 - 1e-4) for p in ps]
        acc = sum((p > .5) == y for p, y in zip(ps, ys) if p != .5) / max(1, sum(p != .5 for p in ps))
        ll = -sum(math.log(p) if y else math.log(1 - p) for p, y in zip(clip, ys)) / n
        brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
        lead_n = sum(1 for x in ls if x)
        lead_acc = sum((x > 0) == y for x, y in zip(ls, ys) if x) / lead_n if lead_n else None
        rows.append({"round": r, "n": n, "acc": _r(acc), "logloss": _r(ll), "brier": _r(brier),
                     "auc": _r(auc(ps, ys)), "a_wins": _r(sum(ys) / n), "leader_acc": _r(lead_acc)})
    return rows


def calibration(ds: list[dict], bins: int = 10) -> dict:
    out = {}
    for name, lo, hi in PHASES:
        acc = [[0, 0.0, 0] for _ in range(bins)]          # n, sum of predictions, A wins
        for d in ds:
            for r, p in d["eval"].items():
                if p is None or not lo <= int(r) <= hi:
                    continue
                b = min(bins - 1, int(p * bins))
                acc[b][0] += 1
                acc[b][1] += p
                acc[b][2] += d["winner"] == "A"
        out[name] = [{"lo": i / bins, "hi": (i + 1) / bins, "n": n,
                      "pred": _r(s / n) if n else None, "obs": _r(w / n) if n else None}
                     for i, (n, s, w) in enumerate(acc)]
    return out


def metric_aucs(ds: list[dict], versions: dict, describe: list[dict]) -> list[dict]:
    rows = []
    for m in describe:
        if m["kind"] != "team":
            continue
        name = m["name"]
        cur = [d for d in ds if d["versions"].get(name) == versions.get(name) and name in d["team"]]
        a = {}
        for r in [str(x) for x in SHOW] + ["end"]:
            sc, ys = [], []
            for d in cur:
                v = d["team"][name].get(r)
                if v is None or v[0] is None or v[1] is None:
                    continue
                sc.append(v[0] - v[1])
                ys.append(d["winner"] == "A")
            a[r] = _r(auc(sc, ys), 3)
        rows.append({"name": name, "title": m["title"], "better": m["better"], "n": len(cur), "auc": a})
    return rows


def queen_rows(ds: list[dict]) -> list[dict]:
    rows = []
    for r in [str(x) for x in SHOW]:
        n = alive = solo = solo_w = 0
        for d in ds:
            v = (d["team"].get("queen_alive") or {}).get(r)
            if not v:
                continue
            for t, (mine, theirs) in (("A", v), ("B", v[::-1])):
                n += 1
                alive += bool(mine)
                if mine and not theirs:
                    solo += 1
                    solo_w += d["winner"] == t
        rows.append({"round": int(r), "team_games": n, "alive": _r(alive / n if n else None, 3),
                     "only_mine": solo, "only_mine_wins": _r(solo_w / solo if solo else None, 3)})
    return rows


def elo_rows(ds: list[dict]) -> list[dict]:
    rows = []
    for name, lo, hi in ELO:
        sub = [d for d in ds if d["elo"] is not None and lo <= d["elo"] < hi]
        er = {x["round"]: x for x in eval_rows(sub)}
        rows.append({"bracket": name, "games": len(sub),
                     "acc": {str(r): er[r].get("acc") for r in (100, 300, 490)},
                     "n": {str(r): er[r].get("n") for r in (100, 300, 490)}})
    return rows


def window(ds: list[dict], versions: dict, describe: list[dict]) -> dict:
    decided = [d for d in ds if d["winner"] in ("A", "B")]
    cur = [d for d in decided if d["versions"].get("eval") == versions.get("eval")]
    endings: dict[str, int] = {}
    for d in ds:
        k = ending(d)
        endings[k] = endings.get(k, 0) + 1
    teams = {t.get("name") for d in ds for t in d["seats"].values() if isinstance(t, dict)}
    return {"games": len(ds), "decided": len(decided), "eval_games": len(cur),
            "battles": len({d["battle"] for d in ds}), "teams": len(teams - {None}),
            "from": min((d["at"] for d in ds if d["at"]), default=None),
            "to": max((d["at"] for d in ds if d["at"]), default=None),
            "eval": eval_rows(cur), "calibration": calibration(cur),
            "metrics": metric_aucs(decided, versions, describe),
            "endings": dict(sorted(endings.items(), key=lambda kv: -kv[1])),
            "queen": queen_rows(decided), "elo": elo_rows(cur)}


def recent(ds: list[dict], n: int = 60) -> list[dict]:
    out = []
    for d in sorted(ds, key=lambda d: -d["ts"])[:n]:
        ev = d["eval"]
        out.append({"id": d["id"], "battle": d["battle"], "at": d["at"], "map": d["map"],
                    "seats": d["seats"], "winner": d["winner"], "end": d["end"], "rounds": d["rounds"],
                    "ending": ending(d),
                    "eval": {str(r): ev.get(r, ev.get(str(r))) for r in (100, 300, 490)}})
    return out


def build(digests: list[dict], versions: dict, describe: list[dict], now: float | None = None) -> dict:
    now = now or time.time()
    ds = [d for d in digests if d]
    out = {"built": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "versions": versions, "windows": {}}
    for name, span in WINDOWS:
        sub = ds if span is None else [d for d in ds if d["ts"] >= now - span]
        out["windows"][name] = window(sub, versions, describe)
    out["recent"] = recent(ds)
    return out
