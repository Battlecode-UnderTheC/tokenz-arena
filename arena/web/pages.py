"""The website (DESIGN.md section 7): server-rendered pages for people, from the same
in-memory state and the same data functions as the JSON API. No frontend build; a little
inline JS for the submit form, the promote button and the server page.

Every page links its JSON twin in the footer, so a person and an agent see the same numbers.
All user-provided text (notes, names, owners, URLs) goes through `e()` (html.escape).
"""

from __future__ import annotations

import json
import re
from html import escape
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from arena import report
from arena.api import http
from arena.state import Arena, result_for
from arena.web import viewer

NAV = [("/", "Leaderboard"), ("/ideas", "Ideas"), ("/games", "Games"), ("/maps", "Maps"),
       ("/queue", "Queue"), ("/server", "Server"), ("/submit", "Submit"), ("/docs", "Docs")]


def e(x) -> str:
    return escape("" if x is None else str(x))


def q(x) -> str:
    """A path segment."""
    return quote(str(x), safe="")


# ---------------------------------------------------------------------------- layout

CSS = """
:root {
  --bg: #ffffff; --fg: #1d1f23; --muted: #6a6f78; --line: #e3e5e8; --soft: #f5f6f8;
  --accent: #2160c4; --win: #1a7f37; --loss: #c62828; --draw: #8a6d00; --chip: #eef1f5;
  --nav: #16181c; --navfg: #e8eaed;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15171a; --fg: #e3e5e8; --muted: #9097a1; --line: #2b2f35; --soft: #1c1f23;
    --accent: #6ea2ff; --win: #4cc26a; --loss: #ff6b6b; --draw: #e0b84a; --chip: #252a31;
    --nav: #0d0e10; --navfg: #e8eaed;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #15171a; --fg: #e3e5e8; --muted: #9097a1; --line: #2b2f35; --soft: #1c1f23;
  --accent: #6ea2ff; --win: #4cc26a; --loss: #ff6b6b; --draw: #e0b84a; --chip: #252a31;
  --nav: #0d0e10; --navfg: #e8eaed;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
nav.top { background: var(--nav); color: var(--navfg); display: flex; flex-wrap: wrap;
  align-items: center; gap: 2px 4px; padding: 6px 16px; }
nav.top .brand { font-weight: 700; margin-right: 14px; color: var(--navfg); }
nav.top a.l { color: #b9bec6; padding: 4px 8px; border-radius: 4px; }
nav.top a.l.on, nav.top a.l:hover { color: #fff; background: #ffffff1a; text-decoration: none; }
main { max-width: 1280px; margin: 0 auto; padding: 16px; }
h1 { font-size: 20px; margin: 4px 0 12px; }
h2 { font-size: 15px; margin: 22px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--line); }
.sub { color: var(--muted); font-size: 13px; }
.muted { color: var(--muted); }
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--line); text-align: left;
  vertical-align: top; white-space: nowrap; }
th { color: var(--muted); font-weight: 600; background: var(--soft); position: sticky; top: 0; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
td.note { white-space: normal; min-width: 220px; max-width: 520px; color: var(--muted); }
tr:hover td { background: var(--soft); }
pre { white-space: pre-wrap; word-break: break-word; background: var(--soft);
  border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; margin: 6px 0;
  font: 12.5px/1.45 ui-monospace, "SF Mono", Menlo, monospace; }
pre.log { max-height: 360px; overflow: auto; }
.chip { display: inline-block; padding: 0 6px; border-radius: 9px; background: var(--chip);
  font-size: 12px; line-height: 18px; white-space: nowrap; }
.W, .win { color: var(--win); } .L, .loss { color: var(--loss); } .D, .draw { color: var(--draw); }
.chip.win { background: color-mix(in srgb, var(--win) 15%, transparent); }
.chip.loss { background: color-mix(in srgb, var(--loss) 15%, transparent); }
.chip.draw { background: color-mix(in srgb, var(--draw) 15%, transparent); }
.chip.warn { background: color-mix(in srgb, var(--draw) 20%, transparent); color: var(--draw); }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 3px 14px; font-size: 13px; }
.kv > div:nth-child(odd) { color: var(--muted); }
.cards { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 4px; }
.card { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; min-width: 210px;
  max-width: 260px; background: var(--soft); font-size: 13px; }
.card .t { font-weight: 600; }
.card .note { color: var(--muted); font-size: 12px; margin-top: 4px; max-height: 3.2em;
  overflow: hidden; }
.idea { border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; margin: 8px 0; }
.idea .head { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; }
form.filters { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: end; margin: 6px 0 12px; }
form.filters label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); }
input, select, textarea, button { font: inherit; color: var(--fg); background: var(--bg);
  border: 1px solid var(--line); border-radius: 5px; padding: 4px 7px; }
button { background: var(--accent); color: #fff; border-color: var(--accent); cursor: pointer;
  padding: 5px 12px; }
button:disabled { opacity: .5; cursor: default; }
form.stack { display: grid; grid-template-columns: max-content minmax(0, 560px); gap: 8px 12px;
  align-items: center; }
form.stack label { color: var(--muted); }
form.stack textarea { min-height: 140px; }
.bar { display: block; height: 100%; background: var(--win); }
.bartrack { display: inline-block; width: 70px; height: 8px; border-radius: 4px;
  background: color-mix(in srgb, var(--loss) 55%, transparent); vertical-align: middle;
  overflow: hidden; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 0 24px; }
.flash { padding: 8px 12px; border-radius: 6px; margin: 8px 0; background: var(--soft);
  border: 1px solid var(--line); }
.flash.err { border-color: var(--loss); color: var(--loss); }
footer { max-width: 1280px; margin: 24px auto 16px; padding: 8px 16px 0;
  border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
@media (max-width: 640px) {
  main { padding: 12px 16px; }
  form.stack { grid-template-columns: 1fr; }
  td.note { min-width: 160px; }
}
"""


def layout(title: str, body: str, json_url: str | list[str] | None = None,
           active: str = "") -> HTMLResponse:
    links = "".join(f'<a class="l{" on" if href == active else ""}" href="{href}">{label}</a>'
                    for href, label in NAV)
    urls = [json_url] if isinstance(json_url, str) else (json_url or [])
    foot = " · ".join(f'<a href="{e(u)}">{e(u)}</a>' for u in urls)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · tokenz-arena</title>
<style>{CSS}</style>
</head>
<body>
<nav class="top"><a class="brand" href="/">tokenz-arena</a>{links}</nav>
<main>
{body}
</main>
<footer>{"JSON: " + foot + " · " if foot else ""}<a href="/llms.txt">/llms.txt</a> ·
<a href="/api/docs">API docs</a></footer>
</body>
</html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------- small pieces

def when(ts: str | None) -> str:
    if not ts:
        return ""
    return f'<span title="{e(ts)}">{e(ts.replace("T", " ")[:16])}</span>'


def sub_link(arena: Arena, sid: str | None, with_bot: bool = False) -> str:
    if not sid:
        return '<span class="muted">-</span>'
    s = arena.subs.get(sid)
    label = e(sid)
    if s and with_bot and not sid.startswith(f"{s.bot}-"):
        label += f' <span class="muted">({e(s.bot)})</span>'
    return f'<a href="/s/{q(sid)}">{label}</a>'


def bot_link(bot: str | None) -> str:
    return f'<a href="/bots/{q(bot)}">{e(bot)}</a>' if bot else ""


def who(owner: str | None, agent: str | None) -> str:
    return e(owner) + (f' <span class="muted">/ {e(agent)}</span>' if agent else "")


def status_chip(status: str | None) -> str:
    cls = {"rated": "win", "done": "win", "rejected": "loss", "retired": "",
           "placing": "draw", "playing": "draw", "queued": "", "checking": ""}.get(status or "", "")
    return f'<span class="chip {cls}">{e(status)}</span>'


def rec(r: dict | None) -> str:
    if not r:
        return '<span class="muted">-</span>'
    return (f'<span class="W">{r.get("W", 0)}</span>-<span class="D">{r.get("D", 0)}</span>-'
            f'<span class="L">{r.get("L", 0)}</span>')


def score_bar(score: float | None) -> str:
    if score is None:
        return '<span class="muted">-</span>'
    return (f'<span class="bartrack"><span class="bar" style="width:{max(0, min(100, score)):.0f}%">'
            f'</span></span> {score:.1f}%')


def parent_verdict(v: dict | None) -> str:
    """'vs parent 5-1-2 beats' from report.verdict's vs_parent."""
    vp = (v or {}).get("vs_parent")
    if not vp:
        return '<span class="muted">no parent</span>' if v else '<span class="muted">-</span>'
    n = vp["W"] + vp["D"] + vp["L"]
    b = vp.get("beats_parent")
    if not n:
        word, cls = "pending", ""
    else:
        word, cls = {True: ("beats", "win"), False: ("lost", "loss"), None: ("tied", "draw")}[b]
        if not (v or {}).get("complete"):
            word += " so far"
    return f'{rec(vp)} <span class="chip {cls}">{word}</span>'


OUTCOME = {"beat_parent": ("beat parent", "win"), "lost_to_parent": ("lost to parent", "loss"),
           "tied_parent": ("tied parent", "draw"), "pending": ("pending", ""),
           "no_parent": ("no parent", ""), "rejected": ("rejected", "loss")}


def outcome_chip(o: str) -> str:
    word, cls = OUTCOME.get(o, (o, ""))
    return f'<span class="chip {cls}">{e(word)}</span>'


_GH = re.compile(r"^https?://(www\.)?github\.com/[^/]+/[^/]+/(tree|blob|commit)/([^/?#]+)")
_FIXED = re.compile(r"^([0-9a-f]{7,40}|v?\d+(\.\d+)*([-.][\w.]+)?)$", re.I)


def source_link(url: str | None) -> str:
    """The source link, flagged 'moving' when it names a branch rather than a commit or tag."""
    if not url:
        return '<span class="muted">-</span>'
    safe = url if url.startswith(("http://", "https://")) else None
    label = e(re.sub(r"^https?://(www\.)?github\.com/", "", url))
    out = f'<a href="{e(safe)}" rel="noopener">{label}</a>' if safe else label
    m = _GH.match(url)
    if m and m.group(2) != "commit" and not _FIXED.match(m.group(3)):
        out += (f' <span class="chip warn" title="links a branch ({e(m.group(3))}), so it may no '
                f'longer match the zip">moving</span>')
    return out


def table(head: list[str], rows: list[list[str]], numeric: set[int] = frozenset(),
          empty: str = "nothing yet") -> str:
    if not rows:
        return f'<p class="muted">{e(empty)}</p>'
    th = "".join(f'<th class="{"n" if i in numeric else ""}">{h}</th>' for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(
        (f'<td class="n">{c}</td>' if i in numeric else
         c if c.startswith("<td") else f"<td>{c}</td>") for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<div class="wrap"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def note_cell(note: str | None, n: int = 160) -> str:
    note = (note or "").strip()
    short = note if len(note) <= n else note[:n].rstrip() + "…"
    return f'<td class="note" title="{e(note)}">{e(short)}</td>'


def game_result(arena: Arena, g, sid: str | None = None) -> str:
    if g.error:
        return f'<span class="chip loss" title="{e(g.error)}">error</span>'
    if sid:
        r = result_for(g, sid)
        cls = {"W": "win", "L": "loss", "D": "draw"}.get(r or "", "")
        return f'<span class="chip {cls}">{e(r)}</span>'
    if g.winner is None:
        return '<span class="chip draw">draw</span>'
    w = g.a if g.winner == "A" else g.b
    return f'{e(g.winner)}: {sub_link(arena, w)}'


def games_table(arena: Arena, games: list, sid: str | None = None) -> str:
    rows = []
    for g in games:
        watch = f'<a href="/games/{g.id}">watch</a>' if g.replay else f'<a href="/games/{g.id}">view</a>'
        opp = ""
        if sid:
            opp = sub_link(arena, g.b if g.a == sid else g.a)
        cells = [f'<a href="/games/{g.id}">{g.id}</a>', when(g.played), e(g.lane), e(g.suite),
                 e(g.map), e(g.seed)]
        if sid:
            cells += ["A" if g.a == sid else "B", opp]
        else:
            cells += [sub_link(arena, g.a), sub_link(arena, g.b)]
        cells += [game_result(arena, g, sid), e(g.rounds), e(g.reason), watch]
        rows.append(cells)
    head = ["#", "played", "lane", "suite", "map", "seed"]
    head += ["seat", "opponent"] if sid else ["seat A", "seat B"]
    head += ["result", "rounds", "end", ""]
    return table(head, rows, numeric={0, 5, 9 if sid else 9}, empty="no games yet")


def json_attr(value) -> str:
    return e(json.dumps(value))


# ---------------------------------------------------------------------------- router

def make_router(arena: Arena) -> APIRouter:
    r = APIRouter(include_in_schema=False)

    def get_sub(sid: str):
        s = arena.subs.get(sid)
        if not s:
            raise HTTPException(404, f"no submission {sid!r}")
        return s

    # ------------------------------------------------------------ leaderboard
    @r.get("/", response_class=HTMLResponse)
    def index():
        rows = []
        for x in http.leaderboard(arena):
            elo = "-" if x["elo"] is None else f'{x["elo"]:.0f}'
            err = "" if x["err"] is None else f' <span class="muted">± {x["err"]:.0f}</span>'
            rows.append([str(x["rank"]), sub_link(arena, x["submission"]), bot_link(x["bot"]),
                         e(x["owner"]), elo + err, str(x["games"]), score_bar(x["score"]),
                         status_chip(x["status"]), rec(x["holdout"]) if x["holdout"] else
                         '<span class="muted">-</span>', note_cell(x["note"], 90)])
        board = table(["#", "submission", "bot", "owner", "elo", "games", "score", "status",
                       "holdout", "note"], rows, numeric={0, 4, 5},
                      empty="No ranked submissions yet. Submit one, or promote a draft.")
        with arena.lock:
            recent = sorted((s for s in arena.subs.values() if s.status != "external"),
                            key=lambda s: s.created, reverse=True)[:10]
        cards = []
        for s in recent:
            v = report.verdict(arena, s.id) if s.status not in ("checking", "rejected") else None
            line = (f'<span class="chip loss">rejected</span>' if s.status == "rejected"
                    else parent_verdict(v) if s.parent else status_chip(s.status))
            cards.append(f'<div class="card"><div class="t">{sub_link(arena, s.id)} '
                         f'<span class="chip">{e(s.lane)}</span></div>'
                         f'<div class="muted">{who(s.owner, s.agent)} · {when(s.created)}</div>'
                         f'<div>{line}</div><div class="note">{e(s.note)}</div></div>')
        strip = (f'<div class="cards">{"".join(cards)}</div>' if cards
                 else '<p class="muted">No submissions yet.</p>')
        body = f"""<h1>Leaderboard</h1>
<p class="sub">Current ranked submission of each active bot. Elo from a Bradley-Terry fit
(reference bot 1000); score is (W + D/2) / games on the public maps.</p>
{board}
<h2>Recent submissions</h2>{strip}"""
        return layout("Leaderboard", body, ["/api/v1/leaderboard", "/api/v1/submissions"], "/")

    # ------------------------------------------------------------ bot line
    @r.get("/bots/{bot}", response_class=HTMLResponse)
    def bot_page(bot: str):
        d = http.bot_detail(arena, bot)
        if d is None:
            raise HTTPException(404, f"no bot {bot!r}")
        ratings = getattr(arena, "ratings", {}) or {}
        rows = []
        for s in d["submissions"]:
            rt = ratings.get(s["id"]) or {}
            elo = f'{rt["elo"]:.0f}' if rt.get("elo") is not None else "-"
            cur = ' <span class="chip win">current</span>' if s["id"] == d.get("current") else ""
            rows.append([sub_link(arena, s["id"]) + cur, when(s["created"]),
                         e(s["lane"]), status_chip(s["status"]), who(s["owner"], s["agent"]),
                         sub_link(arena, s["parent"], with_bot=True), parent_verdict(s.get("verdict")),
                         elo, source_link(s["source"]), note_cell(s["note"], 220)])
        line = table(["submission", "created", "lane", "status", "owner / agent", "parent",
                      "vs parent", "elo", "source", "note"], rows, numeric={7})
        # lineage: ancestry of each submission, across bots, and other bots built on this one
        mine = {s["id"] for s in d["submissions"]}
        lin = []
        for s in reversed(d["submissions"]):
            chain, p, seen = [], s["parent"], set()
            while p and p not in seen and len(chain) < 12:
                seen.add(p)
                chain.append(sub_link(arena, p, with_bot=True))
                p = arena.subs[p].parent if p in arena.subs else None
            lin.append(f'<li>{sub_link(arena, s["id"])}'
                       + (f' ← {" ← ".join(chain)}' if chain else
                          ' <span class="muted">(root)</span>') + "</li>")
        children = sorted((x for x in arena.subs.values() if x.parent in mine and x.bot != bot),
                          key=lambda x: x.created)
        kids = "".join(f'<li>{sub_link(arena, x.id, with_bot=True)} builds on '
                       f'{sub_link(arena, x.parent)}</li>' for x in children)
        h2h = []
        for x in d["head_to_head"]:
            n = x["W"] + x["D"] + x["L"]
            sc = round(100 * (x["W"] + x["D"] / 2) / n, 1) if n else None
            h2h.append([sub_link(arena, x["opponent"], with_bot=True), rec(x), str(n),
                        score_bar(sc),
                        f'<a href="/games?sub={q(d["current"])}&vs={q(x["opponent"])}">games</a>'])
        cur = d.get("current")
        body = f"""<h1>{e(bot)}</h1>
<div class="kv">
<div>owner</div><div>{e(d.get("owner"))}</div>
<div>current</div><div>{sub_link(arena, cur) if cur else '<span class="muted">none (no ranked submission)</span>'}</div>
<div>active</div><div>{"yes" if d.get("active", True) else "no"}</div>
<div>submissions</div><div>{len(d["submissions"])}</div>
</div>
<h2>Submissions</h2>{line}
<div class="grid2">
<div><h2>Lineage</h2><ul>{"".join(lin) or '<li class="muted">-</li>'}</ul>
{f'<p class="sub">Other bots built on this line:</p><ul>{kids}</ul>' if kids else ''}</div>
<div><h2>Head to head{f' ({e(cur)})' if cur else ''}</h2>
{table(["opponent", "W-D-L", "games", "score", ""], h2h, numeric={2},
       empty="no ranked games yet" if cur else "no current submission")}</div>
</div>"""
        return layout(bot, body, f"/api/v1/bots/{q(bot)}")

    # ------------------------------------------------------------ one submission
    @r.get("/s/{sid}", response_class=HTMLResponse)
    def submission_page(sid: str):
        s = get_sub(sid)
        d = http.sub_json(arena, s, detail=True)
        v = d.get("verdict")
        rt = d.get("rating") or {}
        promote = ""
        if s.lane == "draft" and s.status == "done":
            promote = f"""<p><button id="promote" data-id="{e(s.id)}">Promote to ranked</button>
<span id="promote-msg" class="muted"></span></p>
<script>
document.getElementById("promote").onclick = async (ev) => {{
  const b = ev.target, m = document.getElementById("promote-msg");
  if (!confirm("Promote " + b.dataset.id + "? It becomes its bot's current submission.")) return;
  b.disabled = true; m.textContent = "promoting…";
  try {{
    const r = await fetch("/api/v1/submissions/" + encodeURIComponent(b.dataset.id) + "/promote",
                          {{method: "POST"}});
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    m.textContent = "promoted: now " + j.status; setTimeout(() => location.reload(), 800);
  }} catch (err) {{ m.textContent = "failed: " + err.message; b.disabled = false; }}
}};
</script>"""
        info = f"""<div class="kv">
<div>bot</div><div>{bot_link(s.bot)}</div>
<div>owner / agent</div><div>{who(s.owner, s.agent)}</div>
<div>lane / status</div><div>{e(s.lane)} {status_chip(s.status)}</div>
<div>parent</div><div>{sub_link(arena, s.parent, with_bot=True)}</div>
<div>created</div><div>{when(s.created)}</div>
{f'<div>promoted</div><div>{when(s.promoted)} by {e(s.promoted_by)}</div>' if s.promoted else ''}
<div>elo</div><div>{f'{rt["elo"]:.0f} ± {rt.get("err") or 0:.0f}' if rt.get("elo") is not None else '-'}</div>
<div>language</div><div>{e(s.language) or '-'}</div>
<div>source</div><div>{source_link(s.source)}</div>
<div>zip</div><div><a href="/api/v1/submissions/{q(s.id)}/source">download {e(s.id)}.zip</a>
<span class="muted">sha256 {e(s.sha256[:12])}</span></div>
</div>"""
        rejected = ""
        if s.status == "rejected" or s.reason:
            rejected = (f'<div class="flash err"><b>Rejected:</b> {e(s.reason)}</div>')
        log = (f'<details{" open" if s.status == "rejected" else ""}><summary>check log</summary>'
               f'<pre class="log">{e(s.check_log)}</pre></details>') if s.check_log else ""
        verdict_html = ""
        if v:
            vrows = []
            if v.get("vs_parent"):
                vp = v["vs_parent"]
                vrows.append([f'parent {sub_link(arena, vp["parent"], with_bot=True)}', rec(vp),
                              score_bar(vp["score"]), parent_verdict(v)])
            for o in v.get("opponents", []):
                if v.get("vs_parent") and o["opponent"] == v["vs_parent"]["parent"]:
                    continue
                vrows.append([sub_link(arena, o["opponent"], with_bot=True), rec(o),
                              score_bar(o["score"]), ""])
            verdict_html = (f'<h2>Draft verdict <span class="sub">'
                            f'{"complete" if v.get("complete") else "in progress"}</span></h2>'
                            + table(["opponent", "W-D-L", "score", ""], vrows,
                                    empty="opponents not chosen yet"))
        rep_html = ""
        if s.status not in ("checking", "external"):
            rep = report.build(arena, s.id)
            opp = [[sub_link(arena, x["opponent"], with_bot=True), rec(x), score_bar(x["score"]),
                    f'<a href="/games?sub={q(s.id)}&vs={q(x["opponent"])}">games</a>']
                   for x in rep["by_opponent"]]
            mp = [[e(x["map"]), rec(x), score_bar(x["score"]),
                   f'<a href="/games?sub={q(s.id)}&map={q(x["map"].split("/", 1)[-1])}">games</a>']
                  for x in rep["by_map"]]
            deaths = [[e(k), str(n)] for k, n in rep["deaths"].items()]
            cpu = rep["cpu"]
            fmt = (lambda p: f"{p / 1e6:.1f}M" if p is not None else "-")
            losses = [[f'<a href="/games/{x["game"]}">{x["game"]}</a>', e(x["map"]),
                       sub_link(arena, x["opponent"], with_bot=True), e(x["rounds"]), e(x["reason"]),
                       f'<a href="/games/{x["game"]}">watch</a>' if x["replay"] else
                       '<span class="muted">replay pruned</span>'] for x in rep["worst_losses"]]
            hd = rep["holdout"]
            rep_html = f"""<h2>Report <span class="sub">{rep["games"]} games ·
{rec(rep["record"])} · {f'{rep["record"]["score"]}%' if rep["record"]["score"] is not None else ''}
{f' · {rep["rounds_mean"]} rounds mean' if rep["rounds_mean"] else ''}
{f' · holdout {rec(hd)}' if hd else ''}</span></h2>
<div class="grid2">
<div><h2>By opponent</h2>{table(["opponent", "W-D-L", "score", ""], opp)}</div>
<div><h2>By map <span class="sub">worst first</span></h2>{table(["map", "W-D-L", "score", ""], mp)}</div>
<div><h2>Deaths <span class="sub">{rep["deaths_per_game"] if rep["deaths_per_game"] is not None else '-'} per game ·
{rep["errors"]} errors</span></h2>{table(["cause", "count"], deaths, numeric={1}, empty="no deaths recorded")}</div>
<div><h2>CPU</h2><div class="kv">
<div>p99 (worst game)</div><div>{fmt(cpu["p99_max"])}</div>
<div>max</div><div>{fmt(cpu["max"])}</div>
<div>cap</div><div>{fmt(cpu["cap"])}</div>
<div>headroom</div><div>{f'{100 * cpu["headroom"]:.0f}%' if cpu["headroom"] is not None else '-'}</div>
</div></div>
</div>
<h2>Worst losses</h2>{table(["game", "map", "opponent", "rounds", "end", ""], losses, numeric={0, 3},
                             empty="no losses")}"""
        games = http.find_games(arena, sub=s.id, limit=500)
        body = f"""<h1>{e(s.id)}</h1>
{rejected}
<div class="grid2"><div>{info}{promote}</div>
<div><h2 style="margin-top:0">Note</h2><pre>{e(s.note) or '<span class="muted">(no note)</span>'}</pre></div></div>
{log}
{verdict_html}
{rep_html}
<h2>Games <span class="sub">{len(games)}{"+" if len(games) == 500 else ""} ·
<a href="/games?sub={q(s.id)}">filter</a></span></h2>
{games_table(arena, games, sid=s.id)}"""
        return layout(s.id, body, [f"/api/v1/submissions/{q(s.id)}",
                                   f"/api/v1/submissions/{q(s.id)}/report",
                                   f"/api/v1/games?sub={q(s.id)}"])

    # ------------------------------------------------------------ games
    @r.get("/games", response_class=HTMLResponse)
    def games_page(sub: str = "", vs: str = "", map: str = "", suite: str = "", lane: str = "",
                   result: str = "", limit: int = 100):
        limit = max(1, min(limit, 1000))
        games = http.find_games(arena, sub or None, vs or None, map or None, suite or None,
                                lane or None, result or None, limit)
        params = {k: v for k, v in dict(sub=sub, vs=vs, map=map, suite=suite, lane=lane,
                                        result=result).items() if v}
        subs = sorted({x for g in arena.games for x in (g.a, g.b)})
        maps_ = sorted({g.map for g in arena.games})

        def opt(values, cur):
            return "".join(f'<option value="{e(x)}"{" selected" if x == cur else ""}>'
                           f'{e(x) or "any"}</option>' for x in [""] + list(values))
        form = f"""<form class="filters" method="get" action="/games">
<label>submission<input name="sub" value="{e(sub)}" list="subs" placeholder="any"></label>
<label>vs<input name="vs" value="{e(vs)}" list="subs" placeholder="any"></label>
<label>map<select name="map">{opt(maps_, map)}</select></label>
<label>suite<select name="suite">{opt(["official", "rotating", "holdout", "server", "check"], suite)}</select></label>
<label>lane<select name="lane">{opt(["draft", "placement", "holdout", "continuous", "check", "server"], lane)}</select></label>
<label>result (for submission)<select name="result">{opt(["W", "D", "L"], result)}</select></label>
<label>limit<input name="limit" value="{limit}" size="4"></label>
<button type="submit">Filter</button> <a href="/games">clear</a>
<datalist id="subs">{"".join(f'<option value="{e(x)}">' for x in subs)}</datalist>
</form>"""
        api = "/api/v1/games" + ("?" + urlencode({**params, "limit": limit}) if params else "")
        body = (f"<h1>Games</h1>{form}<p class=\"sub\">{len(games)} shown, newest first</p>"
                + games_table(arena, games, sid=sub if sub and sub in arena.subs else None))
        return layout("Games", body, api, "/games")

    @r.get("/games/{gid}", response_class=HTMLResponse)
    def game_page(gid: int):
        g = http.find_game(arena, gid)
        if g is None:
            raise HTTPException(404, f"no game {gid}")
        win = (f'<b class="W">{e(g.a if g.winner == "A" else g.b)}</b> wins' if g.winner else
               '<b class="L">error</b>' if g.error else '<b class="D">draw</b>')
        verdict = (f'{win} after {e(g.rounds)} rounds'
                   + (f' <span class="m">({e(g.reason)})</span>' if g.reason else ""))
        seats = (f'<span class="sa">A</span> <a href="/s/{q(g.a)}">{e(g.a)}</a> vs '
                 f'<span class="sb">B</span> <a href="/s/{q(g.b)}">{e(g.b)}</a>')
        links = (f'<a href="/games?sub={q(g.a)}&vs={q(g.b)}">this pairing</a> · '
                 + (f'<a href="/api/v1/games/{g.id}/replay">replay</a> · ' if g.replay else "")
                 + f'<a href="/api/v1/games/{g.id}">JSON</a>')
        header = f"""<div class="gh">
<a class="home" href="/">tokenz-arena</a>
<span>#{g.id}</span><span>{seats}</span><span>{verdict}</span>
<span class="m">{e(g.map)} · {e(g.suite)} · {e(g.lane)} · seed {e(g.seed)} · {when(g.played)}</span>
<span class="r">{links} · <a href="/games">games</a></span>
</div>"""
        head = """<style>
.gh { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; padding: 6px 14px;
  background: #0d0e10; border-bottom: 1px solid #2b2b2b; font-size: 13px; color: #ddd; }
.gh a { color: #6ea2ff; text-decoration: none; } .gh a:hover { text-decoration: underline; }
.gh .home { font-weight: 700; color: #fff; } .gh .m { color: #9d9d9d; }
.gh .r { margin-left: auto; } .gh .W { color: #4cc26a; } .gh .L { color: #ff6b6b; }
.gh .D { color: #e0b84a; }
.gh .sa, .gh .sb { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 11px;
  font-weight: 700; color: #000; } .gh .sa { background: #4fb3a9; } .gh .sb { background: #d18616; }
</style>"""
        if g.replay:
            return HTMLResponse(viewer.page(f"/api/v1/games/{g.id}/replay",
                                            f"{g.a} vs {g.b} on {g.map}", header=header, head=head))
        body = f"""<h1>Game {g.id}</h1><div class="kv">
<div>seats</div><div>A {sub_link(arena, g.a)} vs B {sub_link(arena, g.b)}</div>
<div>verdict</div><div>{verdict}</div>
<div>map</div><div>{e(g.map)} ({e(g.suite)}), seed {e(g.seed)}</div>
<div>lane</div><div>{e(g.lane)}</div><div>played</div><div>{when(g.played)}</div>
{f'<div>error</div><div>{e(g.error)}</div>' if g.error else ''}
</div><p class="muted">The replay of this game is no longer kept (retention), so it can't be
watched; its verdict and stats are below.</p>
<pre>{e(json.dumps(g.stats, indent=1))}</pre>"""
        return layout(f"Game {g.id}", body, f"/api/v1/games/{g.id}", "/games")

    # ------------------------------------------------------------ maps
    @r.get("/maps", response_class=HTMLResponse)
    def maps_page():
        rows = []
        for m in http.map_stats(arena):
            n = m["games"]
            decided = m["seat_a_wins"] + m["seat_b_wins"]
            adv = f'{100 * m["seat_a_wins"] / decided:.0f}%' if decided else "-"
            ends = ", ".join(f"{e(k)} {v}" for k, v in sorted(m["ends"].items(), key=lambda x: -x[1]))
            preview = (f'<a href="/maps/{q(m["suite"])}/{q(m["map"])}">preview</a>'
                       if _map_file(arena, m["suite"], m["map"]) else "")
            rows.append([e(m["suite"]), e(m["map"]), str(n), str(m["seat_a_wins"]),
                         str(m["seat_b_wins"]), str(m["draws"]), adv, str(m["rounds_mean"]),
                         f'<td class="note">{ends}</td>', preview,
                         f'<a href="/games?map={q(m["map"])}&suite={q(m["suite"])}">games</a>'])
        played = {(m["suite"], m["map"]) for m in http.map_stats(arena)}
        unplayed = []
        for suite in ("official", "rotating"):
            d = arena.cfg.map_dir(suite)
            for p in sorted(d.glob("*.map")) if d and d.is_dir() else []:
                if (suite, p.stem) not in played:
                    unplayed.append(f'<a href="/maps/{q(suite)}/{q(p.stem)}">{e(suite)}/{e(p.stem)}</a>')
        body = f"""<h1>Maps</h1>
<p class="sub">Every game except checks and holdout. "A wins %" is seat A's share of decided
games: the first-seat advantage.</p>
{table(["suite", "map", "games", "A wins", "B wins", "draws", "A wins %", "rounds mean", "how games end", "", ""],
       rows, numeric={2, 3, 4, 5, 6, 7}, empty="no games yet")}
{f'<h2>Not played yet</h2><p>{" · ".join(unplayed)}</p>' if unplayed else ''}"""
        return layout("Maps", body, "/api/v1/stats/maps", "/maps")

    @r.get("/maps/{suite}/{name}", response_class=HTMLResponse)
    def map_preview(suite: str, name: str):
        p = _map_file(arena, suite, name)
        if not p:
            raise HTTPException(404, f"no map {suite}/{name}")
        header = (f'<div class="gh"><a class="home" href="/">tokenz-arena</a>'
                  f'<span>{e(suite)}/{e(name)} <span style="color:#9d9d9d">(map editor; changes are not saved)</span></span>'
                  f'<span class="r"><a href="/games?map={q(name)}&suite={q(suite)}">games</a> · '
                  f'<a href="/maps">all maps</a></span></div>')
        head = """<style>.gh { display: flex; gap: 16px; padding: 6px 14px; background: #0d0e10;
border-bottom: 1px solid #2b2b2b; font-size: 13px; color: #ddd; } .gh a { color: #6ea2ff;
text-decoration: none; } .gh .home { font-weight: 700; color: #fff; } .gh .r { margin-left: auto; }</style>"""
        return HTMLResponse(viewer.map_page(p.read_text(), f"{suite}/{name}", header=header,
                                            head=head))

    # ------------------------------------------------------------ ideas
    @r.get("/ideas", response_class=HTMLResponse)
    def ideas_page(request: Request, bot: str = "", outcome: str = "", limit: int = 50):
        text = request.query_params.get("q", "")   # `q` is the path-quoting helper here
        limit = max(1, min(limit, 500))
        found = http.ideas(arena, text, bot or None, outcome or None, limit)
        outs = ["beat_parent", "lost_to_parent", "tied_parent", "no_parent", "pending",
                "rejected", "promoted"]
        sel = "".join(f'<option value="{o}"{" selected" if o == outcome else ""}>'
                      f'{e(OUTCOME.get(o, (o,))[0])}</option>' for o in outs)
        bots = "".join(f'<option value="{e(b)}"{" selected" if b == bot else ""}>{e(b)}</option>'
                       for b in sorted(arena.bots))
        form = f"""<form class="filters" method="get" action="/ideas">
<label>search notes<input name="q" value="{e(text)}" size="32" placeholder="e.g. corridor farm"
 autofocus></label>
<label>bot<select name="bot"><option value="">any</option>{bots}</select></label>
<label>outcome<select name="outcome"><option value="">any</option>{sel}</select></label>
<button type="submit">Search</button> <a href="/ideas">clear</a></form>"""
        items = []
        for x in found:
            v = x["verdict"] or {}
            opp = " ".join(f'<span class="chip">{e(o["opponent"])} {rec(o)}</span>'
                           for o in v.get("opponents", [])
                           if o["opponent"] != (x["parent"] or ""))
            items.append(f"""<div class="idea"><div class="head">
<b>{sub_link(arena, x["id"])}</b> {outcome_chip(x["outcome"])}
{'<span class="chip win">promoted</span>' if x["promoted"] and x["outcome"] != "rejected" else ''}
<span class="muted">{who(x["owner"], x["agent"])} · {when(x["created"])}</span>
{f'<span>parent {sub_link(arena, x["parent"], with_bot=True)}: {parent_verdict(v)}</span>' if x["parent"] else ''}
</div>
{f'<div class="flash err">{e(x["reason"])}</div>' if x["reason"] else ''}
<pre>{e(x["note"]) or '<span class="muted">(no note)</span>'}</pre>
{f'<div class="sub">vs top: {opp}</div>' if opp else ''}
{f'<div class="sub">source: {source_link(x["source"])}</div>' if x["source"] else ''}
</div>""")
        params = {k: v for k, v in dict(q=text, bot=bot, outcome=outcome).items() if v}
        api = "/api/v1/ideas" + ("?" + urlencode(params) if params else "")
        body = (f'<h1>Ideas</h1><p class="sub">Every submission\'s note with what happened to it: '
                f'the team\'s experiment log, newest first.</p>{form}'
                f'<p class="sub">{len(found)} found</p>'
                + ("".join(items) or '<p class="muted">Nothing matches.</p>'))
        return layout("Ideas", body, api, "/ideas")

    # ------------------------------------------------------------ submit
    @r.get("/submit", response_class=HTMLResponse)
    def submit_page(request: Request):
        c = http.caller(request)
        bots = "".join(f'<option value="{e(b)}">' for b in sorted(arena.bots))
        parents = "".join(f'<option value="{e(s.id)}">{e(s.bot)} · {e(s.status)}</option>'
                          for s in sorted(arena.subs.values(), key=lambda s: s.created, reverse=True)
                          if s.status not in ("external", "rejected"))
        owner_row = ("""<label for="owner">owner</label><input id="owner" name="owner"
 placeholder="only for shared (tagged) machines">""" if c.tagged else "")
        body = f"""<h1>Submit a bot</h1>
<p>Submitting as <b>{e(c.owner)}</b>{f' ({e(c.name)})' if c.name and c.name != c.owner else ''}
<span class="muted">· {'no Tailscale user login here (local or tagged device), so you may set owner' if c.tagged else 'from your Tailscale login'}</span></p>
<form id="sub" class="stack" method="post" action="/api/v1/submissions" enctype="multipart/form-data">
<label for="file">zip</label><input id="file" type="file" name="file" accept=".zip" required>
<label for="bot">bot</label><input id="bot" name="bot" list="bots" required
 placeholder="the bot's name; its submissions form a line">
<label for="parent">parent</label><input id="parent" name="parent" list="parents"
 placeholder="the submission this builds on (optional)">
<label for="note">note</label><textarea id="note" name="note"
 placeholder="The hypothesis and what changed (markdown)"></textarea>
<label for="source">source</label><input id="source" name="source"
 placeholder="https://github.com/org/repo/tree/<sha>/bots/name (optional)">
<label for="lane">lane</label><select id="lane" name="lane">
<option value="draft" selected>draft: plays its parent and the top 3, unranked</option>
<option value="ranked">ranked: full placement, joins the leaderboard</option></select>
<label for="agent">agent</label><input id="agent" name="agent"
 placeholder="optional label, e.g. worker-3">
{owner_row}
<span></span><span><button type="submit">Submit</button></span>
</form>
<div id="out"></div>
<datalist id="bots">{bots}</datalist><datalist id="parents">{parents}</datalist>
<p class="sub">Agents: <code>curl -F file=@bot.zip -F bot=NAME -F parent=ID -F note=... \\
http://&lt;arena&gt;/api/v1/submissions</code>. See <a href="/llms.txt">/llms.txt</a>.</p>
<script>
const f = document.getElementById("sub"), out = document.getElementById("out");
f.onsubmit = async (ev) => {{
  ev.preventDefault();
  const b = f.querySelector("button"); b.disabled = true;
  out.className = "flash"; out.textContent = "uploading…";
  const fd = new FormData(f);
  for (const k of ["parent", "source", "agent", "owner"]) if (fd.get(k) === "") fd.delete(k);
  try {{
    const r = await fetch(f.action, {{method: "POST", body: fd}});
    const j = await r.json();
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail));
    out.textContent = "";
    const a = document.createElement("a");
    a.href = "/s/" + encodeURIComponent(j.id); a.textContent = j.id;
    out.append(j.duplicate ? "Already submitted as " : "Submitted: ", a,
               " (" + j.status + "). The draft verdict follows in a few minutes.");
  }} catch (err) {{
    out.className = "flash err"; out.textContent = "Rejected: " + err.message;
  }} finally {{ b.disabled = false; }}
}};
</script>"""
        return layout("Submit", body, "/api/v1/submissions", "/submit")

    # ------------------------------------------------------------ queue
    @r.get("/queue", response_class=HTMLResponse)
    def queue_page():
        qd = http.queue(arena)
        run = [[sub_link(arena, x["a"]), sub_link(arena, x["b"]), e(x["map"]), e(x["seed"]),
                e(x["lane"]), e(x["owner"]), when(x["started"])] for x in qd["running"]]
        wait = [[sub_link(arena, sid), e(w["owner"]), e(w["lane"]), str(w["games_left"]),
                 str(w["games_total"]),
                 score_bar(100 * (1 - w["games_left"] / w["games_total"])) if w["games_total"] else ""]
                for sid, w in sorted(qd["waiting"].items())]
        chk = " · ".join(sub_link(arena, x) for x in qd["checking"])
        body = f"""<h1>Queue</h1>
<p class="sub">{qd["workers"]} judge workers · {len(qd["running"])} games playing ·
{len(qd["waiting"])} submissions waiting · priority: drafts, placement, holdout, continuous.</p>
<h2>Playing now</h2>
{table(["seat A", "seat B", "map", "seed", "lane", "owner", "started"], run, numeric={3}, empty="nothing playing")}
<h2>Checking</h2><p>{chk or '<span class="muted">nothing</span>'}</p>
<h2>Waiting</h2>
{table(["submission", "owner", "lane", "games left", "of", "done"], wait, numeric={3, 4}, empty="nothing waiting")}
<script>setTimeout(() => location.reload(), 15000)</script>"""
        return layout("Queue", body, "/api/v1/queue", "/queue")

    # ------------------------------------------------------------ competition server
    @r.get("/server", response_class=HTMLResponse)
    def server_page():
        body = """<h1>Competition server</h1>
<div id="srv"><p class="muted">loading…</p></div>
<script>
const esc = (x) => String(x ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
const scalar = (v) => v === null || typeof v !== "object";
const show = (v) => {
  if (v === null || v === undefined) return '<span class="muted">-</span>';
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string" && /^[\\w.-]+-\\d+$/.test(v)) return '<a href="/s/' + encodeURIComponent(v) + '">' + esc(v) + '</a>';
  if (typeof v === "string" && /^https?:\\/\\//.test(v)) return '<a href="' + esc(v) + '">' + esc(v) + '</a>';
  if (typeof v === "object") return '<code>' + esc(JSON.stringify(v)) + '</code>';
  return esc(v);
};
const kv = (o) => '<div class="kv">' + Object.entries(o).filter(([k, v]) => scalar(v))
  .map(([k, v]) => '<div>' + esc(k.replace(/_/g, " ")) + '</div><div>' + show(v) + '</div>').join("") + '</div>';
const tab = (rows) => {
  const cols = [...new Set(rows.flatMap((r) => scalar(r) ? ["value"] : Object.keys(r)))];
  return '<div class="wrap"><table><thead><tr>' + cols.map((c) => '<th>' + esc(c.replace(/_/g, " ")) + '</th>').join("") +
    '</tr></thead><tbody>' + rows.map((r) => '<tr>' + cols.map((c) => {
      const v = scalar(r) ? r : r[c];
      if (c === "game" || c === "arena_game" || c === "game_id") return '<td>' + (v ? '<a href="/games/' + encodeURIComponent(v) + '">watch ' + esc(v) + '</a>' : '') + '</td>';
      return '<td>' + show(v) + '</td>';
    }).join("") + '</tr>').join("") + '</tbody></table></div>';
};
const section = (title, v) => {
  if (Array.isArray(v)) return '<h2>' + esc(title) + '</h2>' + (v.length ? tab(v) : '<p class="muted">none</p>');
  if (v && typeof v === "object") {
    const nested = Object.entries(v).filter(([k, x]) => !scalar(x));
    return '<h2>' + esc(title) + '</h2>' + kv(v) + nested.map(([k, x]) => section(title + ": " + k.replace(/_/g, " "), x)).join("");
  }
  return "";
};
(async () => {
  const el = document.getElementById("srv");
  let r;
  try { r = await fetch("/api/v1/server"); } catch (err) { r = null; }
  if (!r || r.status === 404 || !r.ok) {
    el.innerHTML = '<div class="flash">The server bridge is not running on this arena' +
      (r && r.status !== 404 ? ' (/api/v1/server answered ' + r.status + ')' : '') +
      ', so there is nothing to show yet. Once it runs, this page shows the live bot, rank, rating, ' +
      'quota, auto-submit state, the candidate and recent battles.</div>';
    return;
  }
  const d = await r.json();
  let html = d.stale ? '<div class="flash">Cached copy (the competition server rate-limited us).</div>' : '';
  html += '<h2>Summary</h2>' + kv(d);
  for (const [k, v] of Object.entries(d)) if (!scalar(v)) html += section(k.replace(/_/g, " "), v);
  el.innerHTML = html;
})();
</script>"""
        return layout("Server", body, ["/api/v1/server", "/api/v1/server/battles"], "/server")

    return r


def _map_file(arena: Arena, suite: str, name: str):
    """A public map's file (never the holdout pool), or None."""
    if suite in ("holdout", "check") or not re.fullmatch(r"[\w .-]+", name) or name.startswith("."):
        return None
    d = arena.cfg.map_dir(suite)
    p = d / f"{name}.map" if d else None
    return p if p and p.is_file() else None
