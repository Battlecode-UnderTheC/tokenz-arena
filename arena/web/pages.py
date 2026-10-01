"""The website (DESIGN.md section 7): server-rendered pages for people, from the same
in-memory state and the same data functions as the JSON API. No frontend build; a little
inline JS for the submit form, the promote button and the server page.

Every page links its JSON twin in the footer, so a person and an agent see the same numbers.
All user-provided text (notes, names, owners, URLs) goes through `e()` (html.escape).
"""

from __future__ import annotations

import json
import re
import time
from html import escape
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from arena import report
from arena.api import http
from arena.state import Arena, result_for
from arena.web import viewer

NAV = [("/", "Leaderboard"), ("/lineage", "Lineage"), ("/ideas", "Ideas"), ("/games", "Games"), ("/maps", "Maps"),
       ("/queue", "Queue"), ("/server", "Server"), ("/submit", "Submit"), ("/docs", "Docs")]


def e(x) -> str:
    return escape("" if x is None else str(x))


def q(x) -> str:
    """A path segment."""
    return quote(str(x), safe="")


# ---------------------------------------------------------------------------- layout

SRC_CSS = """
.src { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 10px; font-weight: 700;
  letter-spacing: .04em; line-height: 16px; vertical-align: 1px; }
.src.comp { background: #d18616; color: #111; } .src.arena { background: #2f6fd6; color: #fff; }
"""

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
.src { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 10px; font-weight: 700;
  letter-spacing: .04em; line-height: 16px; vertical-align: 1px; }
.src.comp { background: #d18616; color: #111; } .src.arena { background: #2f6fd6; color: #fff; }
.tabs { display: flex; gap: 4px; margin: 4px 0 10px; border-bottom: 1px solid var(--line); }
.tabs a { padding: 6px 12px; border: 1px solid transparent; border-bottom: none;
  border-radius: 6px 6px 0 0; color: var(--muted); }
.tabs a.on { color: var(--fg); border-color: var(--line); background: var(--soft); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin: 8px 0; }
.tile { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: var(--soft); }
.tile .k { color: var(--muted); font-size: 12px; } .tile .v { font-size: 20px; font-weight: 650; margin: 2px 0; }
.tile .d { color: var(--muted); font-size: 12px; }
.gmaps a { display: inline-block; margin: 1px 2px 1px 0; padding: 0 5px; border-radius: 4px;
  font-size: 12px; background: var(--chip); color: var(--fg); }
.gmaps a.W { background: color-mix(in srgb, var(--win) 22%, transparent); }
.gmaps a.L { background: color-mix(in srgb, var(--loss) 22%, transparent); }
.check-ok { color: var(--win); } .check-no { color: var(--loss); }
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
button.small { padding: 1px 8px; font-size: 12px; vertical-align: 2px; }
ul.tree { list-style: none; margin: 0; padding-left: 22px; border-left: 1px dashed var(--line); }
ul.tree.root { padding-left: 0; border-left: none; }
ul.tree li { margin: 3px 0; }
ul.tree summary { cursor: pointer; list-style-position: outside; }
.node .tnote { color: var(--muted); font-size: 12px; margin: 1px 0 0 2px; white-space: normal; }
.node.hl { background: color-mix(in srgb, var(--accent) 18%, transparent); border-radius: 4px; padding: 1px 4px; }
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


_VERSIONS: dict = {"at": 0.0, "names": {}}


def server_version_names(arena: Arena) -> dict[str, str]:
    """{server submission id: its name} for our versions on the competition server
    (from the bridge's cached list, refreshed at most every two minutes)."""
    if time.time() - _VERSIONS["at"] > 120:
        _VERSIONS["at"] = time.time()
        try:
            from arena.server import bridge
            _VERSIONS["names"] = {str(s["id"]): s.get("name") or "" for s in bridge.get(arena).server_subs()}
        except Exception:
            pass
    return _VERSIONS["names"]


def side_label(arena: Arena, sid: str | None) -> str:
    """Plain-text name of a seat: a competition team's name, 'us · <revision>' for one of our
    server versions, else the arena submission id."""
    if not sid:
        return "-"
    if sid.startswith("server:"):
        return sid.removeprefix("server:")
    if sid.startswith("server-version:"):
        ver = sid.removeprefix("server-version:")
        return f"us · {server_version_names(arena).get(ver) or 'v' + ver}"
    return sid


def lineage_root(arena: Arena, sid: str) -> str:
    """The oldest ancestor of a submission (following parents within the arena)."""
    seen = set()
    while sid in arena.subs and arena.subs[sid].parent in arena.subs and sid not in seen:
        seen.add(sid)
        sid = arena.subs[sid].parent
    return sid


def is_comp(g) -> bool:
    """A real battle downloaded from the competition server (not played by our judge)."""
    return g.lane == "server"


def src_badge(g) -> str:
    return ('<span class="src comp" title="a real battle on the competition server">COMPETITION</span>'
            if is_comp(g) else
            '<span class="src arena" title="played by the arena\'s own judge">ARENA</span>')


def our_seat(g) -> str | None:
    """Our seat in a competition game (from the replay's bot ids)."""
    return (g.meta or {}).get("our_seat") if is_comp(g) else None


def sub_link(arena: Arena, sid: str | None, with_bot: bool = False) -> str:
    if not sid:
        return '<span class="muted">-</span>'
    if sid.startswith("server:"):
        team = sid.removeprefix("server:")
        return (f'<a class="team" href="/games?source=competition&vs={q(sid)}" '
                f'title="a team on the competition server">{e(team)}</a>')
    if sid.startswith("server-version:"):
        return (f'<a href="/server"><b>us</b></a> <span class="muted">'
                f'{e(side_label(arena, sid).removeprefix("us · "))}</span>')
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


def score_ci(r: dict) -> str:
    """Score bar plus the 95% interval, and a 'noise' chip when it includes 50%."""
    out = score_bar(r.get("score"))
    ci = r.get("ci")
    if ci:
        out += f' <span class="muted" title="95% Wilson interval">({ci[0]:.0f}-{ci[1]:.0f})</span>'
        if r.get("significant") is False:
            out += ' <span class="chip" title="the interval includes 50%: consistent with no difference">noise</span>'
    return out


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


def comp_games_table(arena: Arena, games: list) -> str:
    """Competition games from our side: opponent, kind, our seat and our result."""
    rows = []
    for g in games:
        m = g.meta or {}
        seat = our_seat(g)
        us = g.a if seat == "A" else g.b if seat == "B" else None
        res = result_for(g, us) if us else None
        cls = {"W": "win", "L": "loss", "D": "draw"}.get(res or "", "")
        word = {"W": "WIN", "L": "LOSS", "D": "DRAW"}.get(res or "", "?")
        opp = g.b if seat == "A" else g.a
        rows.append([f'<a href="/games/{g.id}">{g.id}</a>', when(g.played),
                     f'<span class="chip">{"ranked" if m.get("ranked") else "unranked"}</span>',
                     sub_link(arena, opp), sub_link(arena, us) if us else "-", e(seat or "?"),
                     e(m.get("map_name") or g.map), f'<span class="chip {cls}">{word}</span>',
                     e(g.rounds), e(g.reason),
                     f'<a href="/games/{g.id}">watch</a>' if g.replay else ""])
    return table(["#", "played", "kind", "opponent", "our bot", "our seat", "map", "result",
                  "rounds", "end", ""], rows, numeric={0, 8},
                 empty="no competition games downloaded yet")


def games_table(arena: Arena, games: list, sid: str | None = None) -> str:
    rows = []
    for g in games:
        watch = f'<a href="/games/{g.id}">watch</a>' if g.replay else f'<a href="/games/{g.id}">view</a>'
        opp = ""
        if sid:
            opp = sub_link(arena, g.b if g.a == sid else g.a)
        cells = [f'<a href="/games/{g.id}">{g.id}</a> {src_badge(g)}', when(g.played), e(g.lane),
                 e(g.suite), e(g.map), e(g.seed)]
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
        comp = [g for g in reversed(arena.games) if is_comp(g)][:12]
        comp_html = ""
        if comp:
            chips = []
            for g in comp:
                seat = our_seat(g)
                res = result_for(g, g.a if seat == "A" else g.b) if seat else None
                opp = side_label(arena, g.b if seat == "A" else g.a)
                chips.append(f'<a class="{e(res or "")}" href="/games/{g.id}" '
                             f'title="{e((g.meta or {}).get("map_name") or g.map)} vs {e(opp)}">{e(res or "?")}</a>')
            ours = side_label(arena, comp[0].a if our_seat(comp[0]) == "A" else comp[0].b)
            comp_html = (f'<div class="flash"><span class="src comp">COMPETITION</span> live bot '
                         f'<b>{e(ours.removeprefix("us · "))}</b> · last {len(comp)} server games '
                         f'<span class="gmaps">{"".join(chips)}</span> · <a href="/server">server page</a></div>')
        body = f"""<h1>Leaderboard <span class="src arena">ARENA</span></h1>
{comp_html}
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
    def submission_page(sid: str, request: Request):
        s = get_sub(sid)
        me = http.caller(request).owner
        can_edit = me in (s.owner, "local")
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
<div>parent</div><div>{sub_link(arena, s.parent, with_bot=True)}
{'<button class="small" onclick="setParent()">change</button>' if can_edit else ''}
{f' <span class="muted" title="{e(", ".join(f"{h['parent'] or 'none'} (until {h['at'][:16]}, changed by {h['by']})" for h in s.parent_history))}">(was {e(s.parent_history[-1]["parent"] or "none")})</span>' if s.parent_history else ''}
<script>
async function setParent() {{
  const v = prompt("New parent submission id (empty for none). If it hasn't played that one yet, the arena plays those games and the verdict follows.", {json.dumps(s.parent or "")});
  if (v === null) return;
  const r = await fetch("/api/v1/submissions/{q(s.id)}", {{method: "PATCH",
    headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{parent: v.trim() || null}})}});
  if (r.ok) location.reload(); else alert((await r.json()).detail || r.status);
}}
</script></div>
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
                              score_ci(vp), parent_verdict(v)])
            for o in v.get("opponents", []):
                if v.get("vs_parent") and o["opponent"] == v["vs_parent"]["parent"]:
                    continue
                vrows.append([sub_link(arena, o["opponent"], with_bot=True), rec(o),
                              score_ci(o), ""])
            verdict_html = (f'<h2>Draft verdict <span class="sub">'
                            f'{"complete" if v.get("complete") else "in progress"}</span></h2>'
                            + table(["opponent", "W-D-L", "score", ""], vrows,
                                    empty="opponents not chosen yet"))
        rep_html = ""
        if s.status not in ("checking", "external"):
            rep = report.build(arena, s.id)
            opp = [[sub_link(arena, x["opponent"], with_bot=True), rec(x), score_ci(x),
                    f'<a href="/games?sub={q(s.id)}&vs={q(x["opponent"])}">games</a>']
                   for x in rep["by_opponent"]]
            mp = [[e(x["map"]), rec(x), score_ci(x),
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
<div><h2>By seat and by how games ended</h2>
{table(["", "W-D-L", "score"], [[f"seat {k}", rec(x), score_ci(x)] for k, x in rep["by_seat"].items()]
       + [[e(k), rec(x), score_ci(x)] for k, x in rep["by_end"].items()], empty="no games yet")}
<h2>Economy <span class="sub">mean dragons alive</span></h2>
{table(["round", "ours", "theirs", "games"], [[k, str(x["dragons"]), str(x["opponent_dragons"]), str(x["games"])]
       for k, x in rep["economy"].items() if x], numeric={1, 2, 3}, empty="no games reached round 50")}
{f'<p class="muted">{rep["historical_games"]} games from an older judge version are kept but not counted.</p>' if rep.get("historical_games") else ''}
</div>
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
<div><h2 style="margin-top:0">Note {'<button class="small" onclick="editNote()">edit</button>' if can_edit else ''}
<a class="sub" href="/lineage?root={q(lineage_root(arena, s.id))}#{q(s.id)}">lineage</a></h2>
<pre id="note">{e(s.note) or '<span class="muted">(no note)</span>'}</pre>
<div id="noteedit" style="display:none"><textarea id="notetext" style="width:100%;min-height:160px">{e(s.note)}</textarea>
<button onclick="saveNote()">save</button> <button class="small" onclick="location.reload()">cancel</button>
<span id="notemsg" class="muted"></span></div>
{f'<details><summary class="muted">{len(s.note_history)} earlier version(s)</summary>' + "".join(
    f'<p class="muted">{when(h["at"])} by {e(h["by"])}</p><pre>{e(h["note"])}</pre>' for h in reversed(s.note_history))
  + '</details>' if s.note_history else ''}
<script>
function editNote() {{ document.getElementById("note").style.display = "none";
  document.getElementById("noteedit").style.display = "block"; }}
async function saveNote() {{
  const r = await fetch("/api/v1/submissions/{q(s.id)}", {{method: "PATCH",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{note: document.getElementById("notetext").value}})}});
  if (r.ok) location.reload();
  else document.getElementById("notemsg").textContent = (await r.json()).detail || r.status;
}}
</script></div></div>
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
                   result: str = "", limit: int = 100, source: str = ""):
        limit = max(1, min(limit, 1000))
        if source not in ("arena", "competition", "all"):
            source = "competition" if (lane == "server" or vs.startswith("server:")) else "arena"
        games = http.find_games(arena, sub or None, vs or None, map or None, suite or None,
                                lane or None, result or None, limit,
                                source=None if source == "all" else source)
        params = {k: v for k, v in dict(sub=sub, vs=vs, map=map, suite=suite, lane=lane,
                                        result=result, source=source).items() if v}
        subs = sorted({x for g in arena.games for x in (g.a, g.b)})
        maps_ = sorted({g.map for g in arena.games})

        def opt(values, cur):
            return "".join(f'<option value="{e(x)}"{" selected" if x == cur else ""}>'
                           f'{e(x) or "any"}</option>' for x in [""] + list(values))
        form = f"""<form class="filters" method="get" action="/games">
<label>submission<input name="sub" value="{e(sub)}" list="subs" placeholder="any"></label>
<label>vs<input name="vs" value="{e(vs)}" list="subs" placeholder="any"></label>
<label>map<select name="map">{opt(maps_, map)}</select></label>
<label>suite<select name="suite">{opt(["official", "tournament", "server", "custom", "rotating", "check"], suite)}</select></label>
<label>lane<select name="lane">{opt(["draft", "placement", "continuous", "check", "server"], lane)}</select></label>
<label>result (for submission)<select name="result">{opt(["W", "D", "L"], result)}</select></label>
<label>limit<input name="limit" value="{limit}" size="4"></label>
<input type="hidden" name="source" value="{e(source)}">
<button type="submit">Filter</button> <a href="/games?source={e(source)}">clear</a>
<datalist id="subs">{"".join(f'<option value="{e(x)}">' for x in subs)}</datalist>
</form>"""
        api = "/api/v1/games" + ("?" + urlencode({**params, "limit": limit}) if params else "")
        tab = lambda k, label: (f'<a class="{"on" if source == k else ""}" '
                                f'href="/games?source={k}">{label}</a>')
        tabs = (f'<div class="tabs">{tab("arena", "<span class=\"src arena\">ARENA</span> games our judge played")}'
                f'{tab("competition", "<span class=\"src comp\">COMPETITION</span> real battles on the server")}'
                f'{tab("all", "all")}</div>')
        explain = {
            "arena": "Games the arena played with the official judge on poscatgpu2: drafts, "
                     "placement and background games between submissions. These make the ratings.",
            "competition": "Real games our live bot played on the competition server "
                           "(game.battlecode.au), downloaded automatically. They don't count in "
                           "arena ratings; see the <a href=\"/server\">Server</a> page for battles.",
            "all": "Arena and competition games together."}[source]
        listing = (comp_games_table(arena, games) if source == "competition"
                   else games_table(arena, games, sid=sub if sub and sub in arena.subs else None))
        body = (f"<h1>Games</h1>{tabs}<p class=\"sub\">{explain}</p>"
                + (form if source != "competition" else "")
                + f"<p class=\"sub\">{len(games)} shown, newest first</p>" + listing)
        return layout("Games", body, api, "/games")

    @r.get("/games/{gid}", response_class=HTMLResponse)
    def game_page(gid: int):
        g = http.find_game(arena, gid)
        if g is None:
            raise HTTPException(404, f"no game {gid}")
        names = {"A": side_label(arena, g.a), "B": side_label(arena, g.b)}
        win = (f'<b class="W">{e(names[g.winner])}</b> wins' if g.winner else
               '<b class="L">error</b>' if g.error else '<b class="D">draw</b>')
        verdict = (f'{win} after {e(g.rounds)} rounds'
                   + (f' <span class="m">({e(g.reason)})</span>' if g.reason else ""))
        seats = (f'<span class="sa">A</span> {sub_link(arena, g.a)} vs '
                 f'<span class="sb">B</span> {sub_link(arena, g.b)}')
        if is_comp(g):
            m = g.meta or {}
            seat = our_seat(g)
            res = result_for(g, g.a if seat == "A" else g.b) if seat else None
            word = {"W": '<b class="W">WE WON</b>', "L": '<b class="L">WE LOST</b>',
                    "D": '<b class="D">DRAW</b>'}.get(res or "", "")
            bid = m.get("server_battle")
            context = (f'{src_badge(g)} {"ranked" if m.get("ranked") else "unranked"} battle '
                       + (f'<a href="https://game.battlecode.au/visualiser?match={q(bid)}">M{e(bid)}</a>' if bid else "")
                       + f' · we were seat {e(seat or "?")} · {word}')
            links = (f'<a href="/server">server</a> · '
                     + (f'<a href="/api/v1/games/{g.id}/replay">replay</a> · ' if g.replay else "")
                     + f'<a href="/api/v1/games/{g.id}">JSON</a>')
            back = '<a href="/games?source=competition">competition games</a>'
        else:
            lane_words = {"draft": "draft check", "placement": "placement", "continuous": "background game",
                          "holdout": "holdout", "check": "submission check"}
            context = f'{src_badge(g)} {e(lane_words.get(g.lane, g.lane))} on the {e(g.suite)} maps'
            links = (f'<a href="/games?sub={q(g.a)}&vs={q(g.b)}">this pairing</a> · '
                     + (f'<a href="/api/v1/games/{g.id}/replay">replay</a> · ' if g.replay else "")
                     + f'<a href="/api/v1/games/{g.id}">JSON</a>')
            back = '<a href="/games">games</a>'
        header = f"""<div class="gh">
<a class="home" href="/">tokenz-arena</a>
<span>#{g.id}</span><span>{context}</span><span>{seats}</span><span>{verdict}</span>
<span class="m">{e((g.meta or {}).get("map_name") or g.map)} · seed {e(g.seed)} · {when(g.played)}</span>
<span class="r">{links} · {back}</span>
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
                                            f"{names['A']} vs {names['B']} on {g.map}",
                                            header=header, head=head + f"<style>{SRC_CSS}</style>",
                                            team_names=names))
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
    @r.get("/lineage", response_class=HTMLResponse)
    def lineage_page(bot: str = "", root: str = "", hide_rejected: bool = True, drafts: str = "all"):
        forest = http.lineage(arena, root or None, bot or None, hide_rejected)

        def keep(n) -> bool:      # "promoted": only ranked nodes, but keep paths to them
            return drafts != "promoted" or n["lane"] == "ranked" or any(keep(c) for c in n["children"])

        def verdict(n) -> str:
            vp = n.get("vs_parent")
            if not vp or not (vp.get("W") or vp.get("D") or vp.get("L")):
                return ""
            b = vp.get("beats_parent")
            word, cls = (("beats", "win") if b else ("loses to", "loss") if b is False else ("ties", "draw"))
            noise = "" if vp.get("significant") else ' <span class="chip" title="the 95% interval includes 50%">noise</span>'
            return (f'<span class="chip {cls}">{word} parent</span> {rec(vp)} '
                    f'<span class="muted">{vp.get("score")}%</span>{noise}')

        def render(n, depth=0) -> str:
            kids = [c for c in n["children"] if keep(c)]
            star = '<span title="its bot\'s current ranked version">★</span> ' if n["current"] else ""
            elo = f' <span class="muted">elo {n["elo"]:.0f}</span>' if n.get("elo") is not None else ""
            first = (n["note"] or "").strip().splitlines()[0][:140] if n["note"] else ""
            line = (f'<span class="node" id="{e(n["id"])}">{star}{sub_link(arena, n["id"])} '
                    f'<a class="chip" href="/bots/{q(n["bot"])}">{e(n["bot"])}</a> '
                    f'<span class="chip">{e(n["lane"])}</span> {status_chip(n["status"])} {verdict(n)}{elo} '
                    f'<span class="muted">{who(n["owner"], n["agent"])} · {when(n["created"])}</span>'
                    + (f'<div class="tnote">{e(first)}</div>' if first else "") + '</span>')
            if not kids:
                return f'<li>{line}</li>'
            inner = "".join(render(c, depth + 1) for c in kids)
            return (f'<li><details {"open" if depth < 6 else ""}><summary>{line} '
                    f'<span class="muted">({len(kids)} child{"ren" if len(kids) != 1 else ""})</span></summary>'
                    f'<ul class="tree">{inner}</ul></details></li>')

        trees = "".join(render(n) for n in forest if keep(n))
        bots_ = sorted(arena.bots)
        opt = lambda vals, cur: "".join(f'<option value="{e(x)}"{" selected" if x == cur else ""}>{e(x) or "any"}</option>'
                                        for x in [""] + list(vals))
        body = f"""<h1>Lineage</h1>
<p class="sub">Every submission under the one it was built on (its <code>parent</code>), across bots.
Each line shows its verdict against its parent; ★ marks each bot's current ranked version. Lines
that keep beating their parents are where to build next.</p>
<form class="filters" method="get" action="/lineage">
<label>bot<select name="bot">{opt(bots_, bot)}</select></label>
<label>show<select name="drafts"><option value="all"{" selected" if drafts == "all" else ""}>all submissions</option>
<option value="promoted"{" selected" if drafts == "promoted" else ""}>ranked only (and their ancestors)</option></select></label>
<label>rejected<select name="hide_rejected"><option value="true"{" selected" if hide_rejected else ""}>hidden</option>
<option value="false"{"" if hide_rejected else " selected"}>shown</option></select></label>
<button type="submit">Show</button> <a href="/lineage">reset</a>
{f'<span class="muted">subtree of {sub_link(arena, root)} · <a href="/lineage">whole forest</a></span>' if root else ''}
</form>
{f'<ul class="tree root">{trees}</ul>' if trees else '<p class="muted">nothing to show</p>'}
<script>if (location.hash) {{ const el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
  if (el) {{ el.classList.add("hl"); el.scrollIntoView({{block: "center"}}); }} }}</script>"""
        return layout("Lineage", body, "/api/v1/lineage" + (f"?root={q(root)}" if root else f"?bot={q(bot)}" if bot else ""),
                      "/lineage")

    @r.get("/maps", response_class=HTMLResponse)
    def maps_page(request: Request):
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
        from arena.maps import suite_files
        for suite in ("server", "official", "tournament", "custom", "rotating"):
            for p in suite_files(arena.cfg, suite):
                if (suite, p.stem) not in played:
                    unplayed.append(f'<a href="/maps/{q(suite)}/{q(p.stem)}">{e(suite)}/{e(p.stem)}</a>')
        body = f"""<h1>Maps</h1>
<p class="sub">Every game except checks and holdout. "A wins %" is seat A's share of decided
games: the first-seat advantage.</p>
{table(["suite", "map", "games", "A wins", "B wins", "draws", "A wins %", "rounds mean", "how games end", "", ""],
       rows, numeric={2, 3, 4, 5, 6, 7}, empty="no games yet")}
{f'<h2>Not played yet</h2><p>{" · ".join(unplayed)}</p>' if unplayed else ''}
{uploads_html(request)}"""
        return layout("Maps", body, ["/api/v1/stats/maps", "/api/v1/maps"], "/maps")

    def uploads_html(request: Request) -> str:
        from arena import custom_maps
        me = http.caller(request).owner
        mine_or_local = lambda o: o == me or me == "local"
        rows = []
        for m in sorted(custom_maps.meta(arena).values(), key=lambda x: x["created"], reverse=True):
            if m["suite"] != "custom":
                continue
            rm = (f'<button class="small" onclick="rmMap({json.dumps(m["name"])})">remove</button>'
                  if mine_or_local(m["owner"]) else "")
            rows.append([f'<a href="/maps/custom/{q(m["name"])}">{e(m["name"])}</a>', e(m["owner"]),
                         e("x".join(m.get("size") or [])), when(m["created"]),
                         f'<td class="note">{e(m.get("note") or "")}</td>',
                         f'<a href="/games?suite=custom&map={q(m["name"])}">games</a>', rm])
        hidden = custom_maps.hidden_count(arena)
        return f"""<h2>Team maps <span class="sub">uploaded maps every bot plays on</span></h2>
{table(["map", "owner", "size", "uploaded", "note", "", ""], rows, empty="none uploaded yet")}
<p class="muted">{hidden} hidden map(s) uploaded to the holdout (not listed).</p>
<h2>Upload a map</h2>
<p class="sub">A <code>.map</code> file in the unswbc format (make one with the toolkit's map
editor, or <code>worldgen.py</code>). The official judge plays a test game on it first. Public
maps join drafts, placement and continuous games for every bot. <b>Hidden</b> maps go to the
holdout instead: only the top 4 play them, and nobody can see them or their games, so nobody can
tune to them. Uploading as <b>{e(me)}</b>.</p>
<form id="mapform" class="stack" onsubmit="return upMap(event)">
<label>file</label><span><input type="file" name="file" accept=".map,text/plain" required></span>
<label>name</label><span><input name="name" placeholder="from the file name if empty"></span>
<label>note</label><span><input name="note" placeholder="what it tests: chokepoints, open field, ..."></span>
<label>hidden</label><span><input type="checkbox" name="hidden" value="true"> holdout (not listed)</span>
<span></span><span><button type="submit">Upload</button> <span id="mapmsg" class="muted"></span></span>
</form>
<script>
async function upMap(ev) {{
  ev.preventDefault();
  const f = new FormData(document.getElementById("mapform"));
  if (!f.get("hidden")) f.delete("hidden");
  const msg = document.getElementById("mapmsg");
  msg.textContent = "checking with the judge...";
  const r = await fetch("/api/v1/maps", {{method: "POST", body: f}});
  const d = await r.json();
  if (!r.ok) {{ msg.textContent = d.detail || ("HTTP " + r.status); return false; }}
  msg.textContent = "added " + d.name + " (" + d.suite + "); " + d.check;
  setTimeout(() => location.reload(), 1200);
  return false;
}}
async function rmMap(name) {{
  if (!confirm("Remove " + name + "? Its games stay.")) return;
  const r = await fetch("/api/v1/maps/custom/" + encodeURIComponent(name), {{method: "DELETE"}});
  if (r.ok) location.reload(); else alert((await r.json()).detail);
}}
</script>"""

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
    def server_page(request: Request):
        try:
            from arena.server import bridge as bridge_mod, routes as server_routes
            b = bridge_mod.get(arena)
        except Exception:
            b = None
        if b is None or not b.configured:
            body = ('<h1>Competition server</h1><div class="flash">The server bridge isn\'t '
                    'configured on this arena (no server key), so there\'s nothing to show.</div>')
            return layout("Server", body, "/api/v1/server", "/server")
        try:
            d = b.summary()
        except Exception as ex:
            d = {"status": f"error: {ex}"}
        team, live = d.get("team") or {}, d.get("live") or {}
        auto, quota = d.get("auto_submit") or {}, d.get("quota") or {}
        cand = auto.get("candidate") or {}
        rec_ = team.get("record") or {}
        live_arena = live.get("arena") or ""
        live_link = ("" if not live_arena or live_arena.startswith("server-version:")
                     else f' · arena {sub_link(arena, live_arena)}')

        def tile(k, v, dsc=""):
            return f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div><div class="d">{dsc}</div></div>'
        paused = auto.get("paused")
        toggle = (f'<button class="small" onclick="srv(\'{"resume" if paused else "pause"}\')">'
                  f'{"resume" if paused else "pause"}</button>')
        tiles = "".join([
            tile("our live bot", e(live.get("name") or "-"),
                 f'server version {e(live.get("server_version") or "-")}{live_link}<br>'
                 f'{rec(live.get("record"))} on the server'),
            tile("team rank", f'#{e(team.get("rank") or "-")}',
                 f'{e(team.get("name") or "")} · rating {e(team.get("elo") or "-")} '
                 f'(peak {e(team.get("peak") or "-")}) · {e(rec_.get("wins", 0))}-{e(rec_.get("draws", 0))}-'
                 f'{e(rec_.get("losses", 0))} all time'),
            tile("battle quota", f'{e(quota.get("remaining") if quota.get("remaining") is not None else "?")} / 60',
                 "games left this hour" + (f' · more in {e(quota.get("free_in_minutes"))} min'
                                           if quota.get("free_in_minutes") else "")),
            tile("auto-submit", ("<span class='L'>paused</span>" if paused else
                                 "<span class='W'>on</span>" if auto.get("enabled") else "off") + " " + toggle,
                 (f'paused by {e(auto.get("paused_by"))} · ' if paused else "")
                 + f'at most every {e(auto.get("min_hours"))} h · last upload {when(auto.get("last_upload")) or "never"}'),
        ])
        checks = cand.get("checks") or []
        check_rows = [[('<span class="check-ok">✓</span>' if c.get("ok") else '<span class="check-no">✗</span>'),
                       e(c.get("check")), f'<td class="note">{e(c.get("detail") or "")}</td>'] for c in checks]
        failed = cand.get("failed") or []
        gate = (f'<h2>Auto-submit gate <span class="sub">would the arena\'s best replace our live bot?</span></h2>'
                f'<p>Candidate: {sub_link(arena, cand.get("candidate")) if cand.get("candidate") else "<span class=muted>none</span>"}'
                f' · {"<b class=W>passes, will upload</b>" if cand.get("passed") else "<b class=L>held back</b>"}'
                f' <span class="muted">(checked {when(cand.get("at"))})</span></p>'
                + (f'<ul>{"".join(f"<li>{e(x)}</li>" for x in failed)}</ul>' if failed else "")
                + (table(["", "check", "evidence"], check_rows) if check_rows else ""))
        try:
            battles = server_routes._battle_rows(arena, b, 25) if hasattr(server_routes, "_battle_rows") else None
        except Exception:
            battles = None
        if battles is None:
            rows_, _ = b.client.battles(25)
            battles = [server_routes._battle_row(x, b.battle_summary(x["id"]))
                       for x in (rows_ if isinstance(rows_, list) else [])[:25]]
        brows = []
        for x in battles:
            res = x.get("result") or "pending"
            cls = {"win": "win", "loss": "loss", "draw": "draw"}.get(res, "")
            gm = "".join(f'<a class="{e(gg.get("result") or "")}" href="/games/{gg["arena_game"]}" title="watch">'
                         f'{e(gg.get("map") or "?")}</a>' if gg.get("arena_game") else
                         f'<a class="{e(gg.get("result") or "")}">{e(gg.get("map") or "?")}</a>'
                         for gg in x.get("games") or [])
            elo = x.get("elo_change")
            brows.append([when(x.get("at")), f'<span class="chip">{e(x.get("kind"))}</span>',
                          f'{e(x.get("opponent"))} <span class="muted">{e(x.get("opponent_elo") or "")}</span>',
                          f'<span class="chip {cls}">{e(res)}</span>', f'{e(x.get("wins"))}-{e(x.get("losses"))}',
                          ("" if elo in (None, 0) else f'<span class="{"W" if elo > 0 else "L"}">{elo:+d}</span>'),
                          f'<span class="gmaps">{gm}</span>'])
        ups = d.get("uploads") or []
        up_rows = [[when(u.get("at")), sub_link(arena, u.get("arena")), e(u.get("server_version")),
                    e(u.get("status") or u.get("result") or "")] for u in ups]
        dl = d.get("downloads") or {}
        body = f"""<h1>Competition server <span class="src comp">COMPETITION</span></h1>
<p class="sub">Our team on <a href="{e(d.get("server") or "https://game.battlecode.au")}">game.battlecode.au</a>.
These are real battles, played by our live bot against other teams. Arena games (our own judge)
are on the <a href="/games">Games</a> page.{' <b>Cached copy</b>: the server rate-limited us.' if d.get("stale") else ''}</p>
<div class="tiles">{tiles}</div>
<h2>Recent battles <span class="sub">click a map to watch that game</span></h2>
{table(["when", "kind", "opponent (rating)", "result", "W-L", "rating", "games"], brows, empty="no battles yet")}
<p class="sub">{e(dl.get("games"))} games from {e(dl.get("battles"))} battles downloaded · last check {when(dl.get("last_poll"))}
· <a href="/games?source=competition">all competition games</a></p>
{gate}
<h2>Uploads by the arena</h2>
{table(["when", "arena submission", "server version", "status"], up_rows, empty="none yet")}
<script>
async function srv(what) {{
  const r = await fetch("/api/v1/server/" + what, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: "{{}}"}});
  if (r.ok) location.reload(); else alert((await r.json()).detail || r.status);
}}
</script>"""
        return layout("Server", body, ["/api/v1/server", "/api/v1/server/battles", "/api/v1/server/gate"], "/server")

    return r


def _map_file(arena: Arena, suite: str, name: str):
    """A public map's file (never the holdout pool), or None."""
    if suite in ("holdout", "check") or not re.fullmatch(r"[\w .-]+", name) or name.startswith("."):
        return None
    d = arena.cfg.map_dir(suite)
    p = d / f"{name}.map" if d else None
    return p if p and p.is_file() else None
