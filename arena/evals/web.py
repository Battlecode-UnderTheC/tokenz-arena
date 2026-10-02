"""The website's view of the evals (read-only; the `arena evals` process writes the files):
/evals (the report), /evals/games/<id> (a crawled game in the viewer, with the eval bar) and
/api/v1/evals/*."""

from __future__ import annotations

import json
from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from arena import store
from arena.evals import evals_dir, report as R

COLOURS = {"acc": "#2f7fd6", "auc": "#1a9b5c", "leader_acc": "#9097a1"}

CSS = """
.ev-chart { width: 100%; max-width: 760px; height: auto; }
.ev-chart text { font-size: 11px; fill: var(--muted); }
.ev-chart .grid { stroke: var(--line); stroke-width: 1; }
.ev-key span { display: inline-block; margin-right: 14px; font-size: 12px; }
.ev-key i { display: inline-block; width: 14px; height: 3px; vertical-align: middle; margin-right: 4px; }
.ev-cal { display: flex; flex-wrap: wrap; gap: 18px; }
.ev-cal figure { margin: 0; } .ev-cal figcaption { font-size: 12px; color: var(--muted); text-align: center; }
.tabs a { margin-right: 10px; } .tabs a.on { font-weight: 700; text-decoration: none; color: var(--fg); }
td.heat { font-variant-numeric: tabular-nums; }
"""


def e(x) -> str:
    return escape("" if x is None else str(x))


def pct(x, d=0) -> str:
    return "-" if x is None else f"{x * 100:.{d}f}%"


def num(x, d=3) -> str:
    return "-" if x is None else f"{x:.{d}f}"


class Files:
    """report.json and status.json, re-read when they change."""

    def __init__(self, cfg):
        self.dir = evals_dir(cfg)
        self._cache: dict[str, tuple[float, object]] = {}

    def read(self, name: str):
        p = self.dir / name
        try:
            mt = p.stat().st_mtime
        except FileNotFoundError:
            return None
        hit = self._cache.get(name)
        if hit and hit[0] == mt:
            return hit[1]
        v = store.read_json(p, None)
        self._cache[name] = (mt, v)
        return v

    def game(self, gid: int) -> dict | None:
        return store.read_json(self.dir / "games" / f"{int(gid)}.json", None)

    def metrics(self, gid: int) -> dict | None:
        return store.read_json(self.dir / "metrics" / f"{int(gid)}.json", None)


# ---------------------------------------------------------------------------- charts

def line_chart(rows: list[dict], keys: tuple, lo=0.4, hi=1.0, w=760, h=240) -> str:
    pl, pr, pt, pb = 38, 10, 10, 24
    x = lambda r: pl + (r - 10) / 480 * (w - pl - pr)
    y = lambda v: pt + (hi - v) / (hi - lo) * (h - pt - pb)
    out = [f'<svg class="ev-chart" viewBox="0 0 {w} {h}">']
    for v in [round(lo + i * 0.1, 1) for i in range(int(round((hi - lo) / 0.1)) + 1)]:
        out.append(f'<line class="grid" x1="{pl}" x2="{w - pr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text x="{pl - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.0%}</text>')
    for r in (10, 100, 200, 300, 400, 490):
        out.append(f'<text x="{x(r):.1f}" y="{h - 6}" text-anchor="middle">r{r}</text>')
    for k in keys:
        pts = [(x(row["round"]), y(min(hi, max(lo, row[k])))) for row in rows if row.get(k) is not None]
        if len(pts) > 1:
            d = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
            dash = ' stroke-dasharray="5 4"' if k == "leader_acc" else ""
            out.append(f'<polyline fill="none" stroke="{COLOURS[k]}" stroke-width="2"{dash} points="{d}"/>')
    out.append("</svg>")
    return "".join(out)


def calibration_chart(bins: list[dict], title: str, s=200) -> str:
    p = 24
    x = lambda v: p + v * (s - 2 * p)
    y = lambda v: s - p - v * (s - 2 * p)
    n_max = max([b["n"] for b in bins] + [1])
    out = [f'<figure><svg class="ev-chart" style="width:{s}px" viewBox="0 0 {s} {s}">',
           f'<rect x="{p}" y="{p}" width="{s - 2 * p}" height="{s - 2 * p}" fill="none" class="grid" stroke="currentColor" stroke-opacity=".2"/>',
           f'<line x1="{x(0)}" y1="{y(0)}" x2="{x(1)}" y2="{y(1)}" stroke="#9097a1" stroke-dasharray="4 4"/>',
           f'<text x="{s / 2}" y="{s - 4}" text-anchor="middle">eval says</text>',
           f'<text x="10" y="{s / 2}" text-anchor="middle" transform="rotate(-90 10 {s / 2})">A won</text>']
    pts = [b for b in bins if b["n"]]
    if len(pts) > 1:
        out.append('<polyline fill="none" stroke="#2f7fd6" stroke-width="1.5" points="'
                   + " ".join(f"{x(b['pred']):.1f},{y(b['obs']):.1f}" for b in pts) + '"/>')
    for b in pts:
        rad = 2 + 5 * (b["n"] / n_max) ** .5
        out.append(f'<circle cx="{x(b["pred"]):.1f}" cy="{y(b["obs"]):.1f}" r="{rad:.1f}" fill="#2f7fd6">'
                   f'<title>eval {b["lo"]:.0%}-{b["hi"]:.0%}: said {b["pred"]:.0%}, A won {b["obs"]:.0%} of {b["n"]}</title></circle>')
    out.append(f"</svg><figcaption>{e(title)}</figcaption></figure>")
    return "".join(out)


def heat(v, better="higher") -> str:
    if v is None:
        return '<td class="heat n muted">-</td>'
    s = min(1.0, abs(v - .5) * 3)
    good = v > .5 if better != "lower" else v < .5
    rgb = "26,155,92" if good or better == "none" else "198,40,40"
    return f'<td class="heat n" style="background: rgba({rgb},{s * .45:.2f})">{v:.2f}</td>'


# ---------------------------------------------------------------------------- routes

def make_router(arena) -> APIRouter:
    from arena.web.pages import layout, local_time, table

    files = Files(arena.cfg)
    r = APIRouter()

    def teams_of(g: dict) -> dict:
        seats = g.get("seats") or {}
        if seats:
            return {k: (v or {}).get("name") or k for k, v in seats.items()}
        t = g.get("teams") or {}
        return {"A": (t.get("a") or {}).get("name") or "A", "B": (t.get("b") or {}).get("name") or "B"}

    # ------------------------------------------------------------ API
    @r.get("/api/v1/evals", tags=["evals"])
    def evals_report(window: str | None = None):
        """The evals report: how well the eval and each metric predicted real games between other
        teams (per round: accuracy, log loss, Brier, AUC; calibration; metric AUCs; endings;
        queens; Elo brackets), for the windows 5h, 12h, 1d, 3d and all (the game moves fast, so drift shows in hours). `window` picks one."""
        rep = files.read("report.json")
        if rep is None:
            raise HTTPException(404, "no evals report yet (is the evals service running?)")
        if window:
            if window not in rep["windows"]:
                raise HTTPException(400, f"window is one of {list(rep['windows'])}")
            return {**rep, "windows": {window: rep["windows"][window]}}
        return rep

    @r.get("/api/v1/evals/status", tags=["evals"])
    def evals_status():
        """The evals collector's heartbeat: last poll, games stored, pending, last error."""
        return files.read("status.json") or {}

    @r.get("/api/v1/evals/games/{gid}", tags=["evals"])
    def evals_game(gid: int):
        """One crawled game: battle, teams by seat, winner, and its metrics series."""
        g = files.game(gid)
        if g is None:
            raise HTTPException(404, f"no evals game {gid}")
        return {**g, "metrics": files.metrics(gid),
                "replay_url": f"/api/v1/evals/games/{gid}/replay" if (files.dir / "replays" / f"{gid}.replay").exists() else None}

    @r.get("/api/v1/evals/games/{gid}/replay", tags=["evals"])
    def evals_replay(gid: int):
        p = files.dir / "replays" / f"{int(gid)}.replay"
        if not p.exists():
            raise HTTPException(404, "replay not kept")
        from arena.retention import is_gzip
        headers = {"Content-Encoding": "gzip"} if is_gzip(p) else {}
        return FileResponse(p, media_type="application/octet-stream", headers=headers,
                            filename=f"server-{gid}.replay")

    # ------------------------------------------------------------ pages
    @r.get("/evals", response_class=HTMLResponse, include_in_schema=False)
    def evals_page(w: str = "1d"):
        rep = files.read("report.json")
        st = files.read("status.json") or {}
        status = (f'<p class="muted">collector: last poll {local_time(st.get("last_poll"))}, every '
                  f'{int(st.get("poll_seconds") or 0) // 60} min over the top {e(st.get("top_teams"))} teams · '
                  f'{e(st.get("games"))} games stored, {e(st.get("pending"))} computing, '
                  f'{e(st.get("stale"))} to recompute · replays {e(st.get("replays_gb", 0))} GB'
                  + (f' · <b class="L">{e(st["last_error"])}</b>' if st.get("last_error") else "") + "</p>"
                  if st else '<p class="muted">The evals service hasn\'t reported yet.</p>')
        intro = ('<h1>Evals</h1><p>Our in-game metrics checked against real games between <i>other</i> '
                 'teams on the competition server, downloaded continuously by the <code>evals</code> '
                 'service. "Eval" is our P(seat A wins) at each round (<a href="/docs/metrics">metrics</a>, <a href="/docs/evals">how this works</a>).</p>')
        if not rep:
            return layout("Evals", f"<style>{CSS}</style>{intro}{status}", "/api/v1/evals", "/evals")
        if w not in rep["windows"]:
            w = "1d" if "1d" in rep["windows"] else "all"
        W = rep["windows"][w]
        tabs = '<p class="tabs">' + "".join(
            f'<a class="{"on" if k == w else ""}" href="/evals?w={k}">{k}</a>' for k in rep["windows"]) + "</p>"
        summary = (f'<p>{e(W["games"])} games ({e(W["decided"])} decided, {e(W["eval_games"])} judged out of sample: finished after the current eval '
                   f'v{e(rep["versions"].get("eval"))} was fitted{(" on " + local_time(rep["eval_fitted"])) if rep.get("eval_fitted") else ""}) from {e(W["battles"])} battles between {e(W["teams"])} teams, '
                   f'{local_time(W["from"])} to {local_time(W["to"])}. Report built {local_time(rep["built"])}.</p>')
        rows = W["eval"]
        key = ('<p class="ev-key"><span><i style="background:#2f7fd6"></i>accuracy (eval &gt; 50% picks the winner)</span>'
               '<span><i style="background:#1a9b5c"></i>AUC</span>'
               '<span><i style="background:#9097a1"></i>baseline: whoever leads under the round-500 rule now</span></p>')
        chart = line_chart(rows, ("leader_acc", "acc", "auc"))
        by_round = {x["round"]: x for x in rows}
        trows = []
        for rr in R.SHOW:
            x = by_round.get(rr) or {}
            trows.append([f"r{rr}", e(x.get("n", 0)), pct(x.get("acc")), num(x.get("auc"), 2),
                          num(x.get("logloss")), num(x.get("brier")), pct(x.get("leader_acc")), pct(x.get("a_wins"))])
        eval_tbl = table(["round", "games", "accuracy", "AUC", "log loss", "Brier", "leader baseline", "seat A won"],
                         trows, numeric={1, 2, 3, 4, 5, 6, 7})
        cal = '<div class="ev-cal">' + "".join(
            calibration_chart(W["calibration"][name], f"{name} (r{lo}-r{hi})") for name, lo, hi in R.PHASES) + "</div>"
        cols = [str(x) for x in R.SHOW] + ["end"]
        mrows = "".join(
            f'<tr><td>{e(m["title"])} <span class="muted">{e(m["name"])}</span></td><td class="n">{m["n"]}</td>'
            + "".join(heat(m["auc"].get(c), m["better"]) for c in cols) + "</tr>" for m in W["metrics"])
        mtable = ('<div class="wrap"><table><thead><tr><th>metric</th><th class="n">games</th>'
                  + "".join(f'<th class="n">{"r" + c if c != "end" else "end"}</th>' for c in cols)
                  + f"</tr></thead><tbody>{mrows}</tbody></table></div>")
        endings = table(["how it ended", "games", "share"],
                        [[e(k), e(v), pct(v / max(1, W["games"]))] for k, v in W["endings"].items()], numeric={1, 2})
        queen = table(["round", "queens alive", "only my queen alive", "…and I won"],
                      [[f'r{x["round"]}', pct(x["alive"]), e(x["only_mine"]), pct(x["only_mine_wins"])]
                       for x in W["queen"]], numeric={1, 2, 3})
        elo = table(["weaker team's Elo", "games", "accuracy r100", "r300", "r490"],
                    [[e(x["bracket"]), e(x["games"])] + [pct(x["acc"].get(k)) for k in ("100", "300", "490")]
                     for x in W["elo"]], numeric={1, 2, 3, 4})
        recent_rows = []
        for g in rep.get("recent") or []:
            names = {k: (v or {}).get("name") for k, v in (g.get("seats") or {}).items()}
            wname = names.get(g["winner"]) or g["winner"] or "draw"
            ev = g.get("eval") or {}
            recent_rows.append([
                local_time(g["at"]), f'{e(names.get("A") or "?")} <span class="muted">vs</span> {e(names.get("B") or "?")}',
                e(g["map"]), e(wname), e(g["ending"]), e(g["rounds"])]
                + [pct(ev.get(k)) for k in ("100", "300", "490")]
                + [f'<a href="/evals/games/{g["id"]}">watch</a>'])
        recent = table(["finished", "A vs B", "map", "winner", "how", "rounds", "eval r100", "r300", "r490", ""],
                       recent_rows, numeric={5, 6, 7, 8})
        drift_rows = []
        for k, X in rep["windows"].items():
            er = {x["round"]: x for x in X["eval"]}
            drift_rows.append([f'<a href="/evals?w={k}">{e(k)}</a>', e(X["eval_games"])]
                              + [pct(er.get(rr, {}).get("acc")) for rr in (100, 200, 300, 400, 490)]
                              + [num(er.get(100, {}).get("auc"), 2), num(er.get(300, {}).get("auc"), 2)])
        drift = table(["window", "games", "acc r100", "r200", "r300", "r400", "r490", "AUC r100", "AUC r300"],
                      drift_rows, numeric={1, 2, 3, 4, 5, 6, 7, 8})
        body = (f"<style>{CSS}</style>{intro}{status}"
                f'<h2>Across windows <span class="sub">the eval on the most recent games: '
                f'a drop in the short windows means the top teams changed how they play</span></h2>{drift}'
                f"<h2>Window {e(w)}</h2>{tabs}{summary}"
                f"<h2>Does the eval pick the winner?</h2>{key}{chart}{eval_tbl}"
                f'<h2>Calibration <span class="sub">when the eval says x%, does A win x% of the time? '
                f'(dot size: games)</span></h2>{cal}'
                f'<h2>Each metric <span class="sub">AUC of (A − B) for "A wins": 0.5 is a coin flip; '
                f'green helps predict in the expected direction</span></h2>{mtable}'
                f'<div class="grid2"><div><h2>How games end</h2>{endings}</div>'
                f'<div><h2>Queens</h2>{queen}</div></div>'
                f"<h2>By strength</h2>{elo}<h2>Latest games</h2>{recent}")
        return layout("Evals", body, ["/api/v1/evals", "/api/v1/evals/status"], "/evals")

    @r.get("/evals/games/{gid}", response_class=HTMLResponse, include_in_schema=False)
    def evals_game_page(gid: int):
        from arena import metrics as M
        from arena.web import evalstrip, viewer
        g = files.game(gid)
        if g is None:
            raise HTTPException(404, f"no evals game {gid}")
        names = teams_of(g)
        if not (files.dir / "replays" / f"{gid}.replay").exists():
            body = (f"<h1>Server game {gid}</h1><p>{e(names['A'])} vs {e(names['B'])} on {e(g.get('map'))}; "
                    f"the replay is no longer kept.</p><pre>{e(json.dumps(g, indent=1))}</pre>")
            return layout(f"Server game {gid}", body, f"/api/v1/evals/games/{gid}", "/evals")
        w = g.get("winner")
        verdict = (f'<b class="W">{e(names.get(w, w))}</b> wins' if w in ("A", "B") else '<b class="D">draw</b>')
        bid = g.get("battle")
        header = f"""<div class="gh">
<a class="home" href="/">tokenz-arena</a><span>server game {e(gid)}</span>
<span><span class="sa">A</span> {e(names['A'])} vs <span class="sb">B</span> {e(names['B'])}</span>
<span>{verdict} after {e(g.get('rounds'))} rounds <span class="m">({e(g.get('end'))})</span></span>
<span class="m">{e(g.get('map'))} · {local_time(g.get('at'))} · {"ranked" if g.get("ranked") else "unranked"}</span>
<span class="r"><a href="https://game.battlecode.au/visualiser?match={e(bid)}">M{e(bid)}</a> ·
<a href="/api/v1/evals/games/{e(gid)}">JSON</a> · <a href="/evals">evals</a></span></div>"""
        head = """<style>
:root { --arena-header: 46px; }
.gh { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; padding: 6px 14px;
  background: #0d0e10; border-bottom: 1px solid #2b2b2b; font-size: 13px; color: #ddd; }
.gh a { color: #6ea2ff; text-decoration: none; } .gh .home { font-weight: 700; color: #fff; }
.gh .m { color: #9d9d9d; } .gh .r { margin-left: auto; } .gh .W { color: #4cc26a; } .gh .D { color: #e0b84a; }
.gh .sa, .gh .sb { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 11px;
  font-weight: 700; color: #000; } .gh .sa { background: #4fb3a9; } .gh .sb { background: #d18616; }
</style>"""
        m = files.metrics(gid)
        if m and not m.get("error") and m.get("rounds"):
            s_html, s_head = evalstrip.strip(m, names, M.describe())
            header += s_html
            head += s_head + "<style>:root { --arena-header: 84px; }</style>"
        return HTMLResponse(viewer.page(f"/api/v1/evals/games/{gid}/replay",
                                        f"{names['A']} vs {names['B']} on {g.get('map')}",
                                        header=header, head=head, team_names=names))

    return r
