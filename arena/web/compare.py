"""/compare?a=X&b=Y[&map=M][&scope=all|h2h]: two bots side by side from their games' in-game
metrics (arena/metrics/compare.py), and its JSON twin /api/v1/compare."""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from arena.metrics import compare as C

COL = {"a": "#2f7fd6", "b": "#d9822b"}
TITLES = {"eval": "Win chance (eval)", "dragons": "Dragons alive", "total_length": "Total length",
          "longest": "Longest dragon", "board_control": "Board control", "pearl_rate": "Pearl rate (per round)",
          "queen_length": "Queen length", "queen_room": "Queen room", "queen_danger": "Queen's distance to enemy"}
CSS = """
.cmp-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 8px 0 16px; }
.cmp-form label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 2px; }
.cmp-key span { display: inline-block; margin-right: 14px; font-size: 12px; }
.cmp-key i { display: inline-block; width: 18px; height: 0; border-top: 2px solid; vertical-align: middle; margin-right: 4px; }
.cmp-key i.dash { border-top-style: dashed; }
.cmp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 12px 18px; }
.cmp-grid figure { margin: 0; } .cmp-grid figcaption { font-size: 13px; font-weight: 600; margin-bottom: 2px; }
.cmp-chart { width: 100%; height: auto; } .cmp-chart text { font-size: 10px; fill: var(--muted); }
.cmp-chart .grid { stroke: var(--line); } .cmp-chart .phase { stroke: var(--muted); stroke-dasharray: 2 3; opacity: .6; }
td.a { color: #2f7fd6; } td.b { color: #d9822b; } th.a { color: #2f7fd6; } th.b { color: #d9822b; }
.bar { display: inline-block; height: 8px; border-radius: 2px; vertical-align: middle; }
"""


def e(x) -> str:
    return escape("" if x is None else str(x))


def chart(rounds: list[int], lines: list[tuple[list, str, bool]], lo=None, hi=None, pct=False, w=340, h=150) -> str:
    """Lines over rounds (None = a gap): (values, colour, dashed)."""
    vals = [v for vs, _, _ in lines for v in vs if v is not None]
    if not vals:
        return '<p class="muted">no data</p>'
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 1e-9:
        hi = lo + 1
    pl, pr, pt, pb = 34, 6, 6, 18
    x = lambda r: pl + (r - rounds[0]) / (rounds[-1] - rounds[0]) * (w - pl - pr)
    y = lambda v: pt + (hi - v) / (hi - lo) * (h - pt - pb)
    out = [f'<svg class="cmp-chart" viewBox="0 0 {w} {h}">']
    for frac in (0, .5, 1):
        v = lo + frac * (hi - lo)
        label = f"{v:.0%}" if pct else (f"{v:.0f}" if abs(hi - lo) >= 5 else f"{v:.2f}")
        out.append(f'<line class="grid" x1="{pl}" x2="{w - pr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text x="{pl - 4}" y="{y(v) + 3:.1f}" text-anchor="end">{label}</text>')
    for r in (150, 350):
        out.append(f'<line class="phase" x1="{x(r):.1f}" x2="{x(r):.1f}" y1="{pt}" y2="{h - pb}"/>')
    for r in (10, 150, 350, 500):
        out.append(f'<text x="{x(r):.1f}" y="{h - 4}" text-anchor="middle">{r}</text>')
    for vs, colour, dashed in lines:
        seg, segs = [], []
        for r, v in zip(rounds, vs):
            if v is None:
                if seg:
                    segs.append(seg)
                seg = []
            else:
                seg.append(f"{x(r):.1f},{y(v):.1f}")
        if seg:
            segs.append(seg)
        for s in segs:
            if len(s) > 1:
                out.append(f'<polyline fill="none" stroke="{colour}" stroke-width="{1.2 if dashed else 2}"'
                           f'{" stroke-dasharray=\"4 3\"" if dashed else ""} points="{" ".join(s)}"/>')
    out.append("</svg>")
    return "".join(out)


def make_router(arena) -> APIRouter:
    from arena.web.pages import layout, table, sub_link
    r = APIRouter()

    def resolve(sid: str) -> str:
        real = arena.resolve(sid)
        if not real or real not in arena.subs:
            raise HTTPException(404, f"no submission {sid!r}")
        return real

    @r.get("/api/v1/compare", tags=["compare"])
    def compare_api(a: str, b: str, map: str | None = None, scope: str = "all"):
        """Two submissions side by side from their games' in-game metrics: per map, every 10
        rounds (each metric, theirs and their opponents'), their queens (survival, causes of
        death, when), and early/mid/late game. scope: all (each over all its games) or h2h."""
        if scope not in ("all", "h2h"):
            raise HTTPException(400, "scope is all or h2h")
        return C.compare(arena, resolve(a), resolve(b), map or None, scope)

    @r.get("/compare", response_class=HTMLResponse, include_in_schema=False)
    def compare_page(a: str = "", b: str = "", map: str = "", scope: str = "all"):
        subs = sorted((s.id for s in arena.subs.values() if s.status not in ("external", "rejected", "checking")))
        options = "".join(f'<option value="{e(x)}">' for x in subs)
        form = f"""<form class="cmp-form" method="get" action="/compare">
<label>bot A<input name="a" list="cmp-subs" value="{e(a)}" size="22"></label>
<label>bot B<input name="b" list="cmp-subs" value="{e(b)}" size="22"></label>
<label>games<select name="scope"><option value="all"{' selected' if scope == 'all' else ''}>each bot's own games</option>
<option value="h2h"{' selected' if scope == 'h2h' else ''}>only A vs B</option></select></label>
<label>map<input name="map" value="{e(map)}" placeholder="all maps" size="16"></label>
<button>Compare</button><datalist id="cmp-subs">{options}</datalist></form>"""
        intro = ('<h1>Compare</h1><p class="sub">Two bots from their games\' in-game metrics, each from its own seat: '
                 'per map, every 10 rounds, how their queens die, and early (1-150) vs mid (151-350) vs late (351-500).</p>')
        if not (a and b):
            return layout("Compare", f"<style>{CSS}</style>{intro}{form}", "/api/v1/compare", "/compare")
        if scope not in ("all", "h2h"):
            scope = "all"
        A, B = resolve(a), resolve(b)
        d = C.compare(arena, A, B, map or None, scope)
        sa, sb = d["sides"][A], d["sides"][B]
        link = lambda **kw: "/compare?" + urlencode({"a": A, "b": B, "scope": scope, "map": map, **kw})
        if not sa.get("games") and not sb.get("games"):
            return layout("Compare", f"<style>{CSS}</style>{intro}{form}<p>No games with metrics for these yet.</p>",
                          link().replace("/compare", "/api/v1/compare"), "/compare")
        key = (f'<p class="cmp-key"><span><i style="border-color:{COL["a"]}"></i>{e(A)}</span>'
               f'<span><i class="dash" style="border-color:{COL["a"]}"></i>{e(A)}\'s opponents</span>'
               f'<span><i style="border-color:{COL["b"]}"></i>{e(B)}</span>'
               f'<span><i class="dash" style="border-color:{COL["b"]}"></i>{e(B)}\'s opponents</span>'
               '<span class="muted">dotted lines: round 150 and 350</span></p>')

        def head(s, sid, cls):
            if not s.get("games"):
                return f'<td class="{cls}">{sub_link(arena, sid)}</td><td colspan="4" class="muted">no games</td>'
            rcd = s["record"]
            ends = ", ".join(f"{k} {v}" for k, v in list(s["endings"].items())[:4])
            return (f'<td class="{cls}">{sub_link(arena, sid)}</td><td class="n">{s["games"]}</td>'
                    f'<td class="n">{rcd["W"]}-{rcd["D"]}-{rcd["L"]} ({s["score"]}%)</td>'
                    f'<td class="n">{s["avg_rounds"]}</td><td class="note">{e(ends)}</td>')
        summary = ('<div class="wrap"><table><thead><tr><th>bot</th><th class="n">games</th><th class="n">W-D-L</th>'
                   '<th class="n">avg rounds</th><th>how its games ended</th></tr></thead><tbody>'
                   f'<tr>{head(sa, A, "a")}</tr><tr>{head(sb, B, "b")}</tr></tbody></table></div>')
        # per map
        def cell(x):
            if not x:
                return ['<span class="muted">-</span>'] * 4
            return [str(x["games"]), f'{x["score"]}%', str(x["avg_rounds"]), f'{x["queen_died_pct"]}%']
        prow = [[f'<a href="{e(link(map=row["map"]))}">{e(row["map"])}</a>'] + cell(row[A]) + cell(row[B])
                for row in d["per_map"]]
        per_map = table(["map", f"{e(A)} games", "score", "rounds", "queen died",
                         f"{e(B)} games", "score", "rounds", "queen died"], prow, numeric=set(range(1, 9)))
        # round curves
        rounds = d["rounds"]
        figs = []
        for k in d["curves"]:
            ca, cb = (sa.get("curves") or {}).get(k), (sb.get("curves") or {}).get(k)
            if not ca and not cb:
                continue
            lines = []
            if ca:
                lines.append((ca["mine"], COL["a"], False))
                if ca.get("theirs"):
                    lines.append((ca["theirs"], COL["a"], True))
            if cb:
                lines.append((cb["mine"], COL["b"], False))
                if cb.get("theirs"):
                    lines.append((cb["theirs"], COL["b"], True))
            pct = k in ("eval", "board_control")
            figs.append(f'<figure><figcaption>{e(TITLES.get(k, k))}</figcaption>'
                        f'{chart(rounds, lines, 0 if pct else None, 1 if pct else None, pct)}</figure>')
        running = chart(rounds, [([v for v in sa.get("running", [])] or [None] * len(rounds), COL["a"], False),
                                 ([v for v in sb.get("running", [])] or [None] * len(rounds), COL["b"], False)], lo=0)
        figs.append(f'<figure><figcaption>Games still running</figcaption>{running}</figure>')
        # queens
        qa, qb = sa.get("queen") or {}, sb.get("queen") or {}
        surv = chart(rounds, [([None if v is None else v / 100 for v in qa.get("survival", [])] or [None] * len(rounds), COL["a"], False),
                              ([None if v is None else v / 100 for v in qb.get("survival", [])] or [None] * len(rounds), COL["b"], False)],
                     0, 1, pct=True, w=420, h=170)

        def bar(n, total, colour):
            pctv = 100 * n / total if total else 0
            return f'<span class="bar" style="width:{pctv * 1.2:.0f}px;background:{colour}"></span> {n} ({pctv:.0f}%)'
        causes = sorted(set(qa.get("causes", {})) | set(qb.get("causes", {})),
                        key=lambda c: -(qa.get("causes", {}).get(c, 0) + qb.get("causes", {}).get(c, 0)))
        crows = [[e(c), bar(qa.get("causes", {}).get(c, 0), qa.get("died", 0), COL["a"]),
                  bar(qb.get("causes", {}).get(c, 0), qb.get("died", 0), COL["b"])] for c in causes]
        prows = [[e(p), bar(qa.get("phases", {}).get(p, 0), qa.get("died", 0), COL["a"]),
                  bar(qb.get("phases", {}).get(p, 0), qb.get("died", 0), COL["b"])] for p in d["phases"]]
        facts = table(["", e(A), e(B)], [
            ["queen died", f'{qa.get("died", 0)} of {sa.get("games", 0)} ({qa.get("died_pct", 0)}%)',
             f'{qb.get("died", 0)} of {sb.get("games", 0)} ({qb.get("died_pct", 0)}%)'],
            ["median round it died", e(qa.get("median_death_round")), e(qb.get("median_death_round"))],
            ["won when its queen died", f'{qa.get("won_when_queen_died")}%', f'{qb.get("won_when_queen_died")}%'],
            ["won when its queen lived", f'{qa.get("won_when_queen_lived")}%', f'{qb.get("won_when_queen_lived")}%'],
            ["killed the enemy queen", e(qa.get("killed_their_queen")), e(qb.get("killed_their_queen"))]])
        queens = (f'<h2>Queens <span class="sub">survival: share of games with the queen alive by round</span></h2>'
                  f'<div class="grid2"><div>{surv}{facts}</div><div><h3>How it died</h3>'
                  f'{table(["cause", e(A), e(B)], crows, empty="no queen deaths")}'
                  f'<h3>When it died</h3>{table(["phase", e(A), e(B)], prows)}</div></div>')
        # phases
        ph_rows = []
        for k in ["eval"] + [c for c in d["curves"] if c != "eval"]:
            row = [e(TITLES.get(k, k))]
            for p in d["phases"]:
                for s, cls in ((sa, "a"), (sb, "b")):
                    v = ((s.get("phases") or {}).get(p) or {}).get(k) or {}
                    m, t = v.get("mine"), v.get("theirs")
                    fmt = (lambda x: f"{x:.0%}") if k in ("eval", "board_control") else (lambda x: f"{x:.2f}" if abs(x) < 10 else f"{x:.0f}")
                    row.append(f'<td class="n {cls}">{"-" if m is None else fmt(m)}'
                               + (f' <span class="muted">vs {fmt(t)}</span>' if t is not None else "") + "</td>")
            ph_rows.append(row)
        ahead = ["ahead (eval > 50%)"] + [f'<td class="n {cls}">{((s.get("phases") or {}).get(p) or {}).get("ahead_pct")}%</td>'
                                          for p in d["phases"] for s, cls in ((sa, "a"), (sb, "b"))]
        reach = ["games reaching it"] + [f'<td class="n {cls}">{((s.get("phases") or {}).get(p) or {}).get("games_reaching")}</td>'
                                         for p in d["phases"] for s, cls in ((sa, "a"), (sb, "b"))]
        phases = table(["metric"] + [f'<span class="{c}">{p} {e(x)}</span>' for p in d["phases"] for x, c in ((A, "a"), (B, "b"))],
                       [ahead, reach] + ph_rows)
        scope_word = "only their games against each other" if scope == "h2h" else "each bot's own games"
        body = (f"<style>{CSS}</style>{intro}{form}"
                f'<p class="sub">{e(scope_word)} · {"map " + e(map) + " · <a href=\"" + e(link(map="")) + "\">all maps</a>" if map else "all maps"}</p>'
                f"{summary}<h2>Per map</h2>{per_map}"
                f'<h2>Round by round <span class="sub">averages over the games still running</span></h2>{key}'
                f'<div class="cmp-grid">{"".join(figs)}</div>{queens}'
                f'<h2>Early, mid, late <span class="sub">averages over each phase\'s rounds; "vs": its opponents</span></h2>{phases}')
        return layout(f"{A} vs {B}", body, link().replace("/compare", "/api/v1/compare"), "/compare")

    return r
