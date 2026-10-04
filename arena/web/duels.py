"""The Duels pages (docs/DUELS.md): /duels lists duels, /duels/{id} shows one from A's side.
Rendered in the browser from /api/v1/duels (the website forwards it to the engine), and
refreshed while a duel is still playing."""

from __future__ import annotations

from html import escape as e

from fastapi import APIRouter

JS_COMMON = r"""
const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const bot = (id) => id.startsWith("eph-") ? `<span class="chip">${esc(id)}</span>` : `<a href="/s/${encodeURIComponent(id)}">${esc(id)}</a>`;
const wdl = (r) => r ? `<span class="W">${r.W}</span>-<span class="D">${r.D}</span>-<span class="L">${r.L}</span>` : "";
const score = (r) => !r || r.score == null ? "-" : `${r.score}%` + (r.ci ? ` <span class="sub">(${r.ci[0]}-${r.ci[1]})</span>` : "");
const status = (s) => {
  const cls = {done: "win", failed: "loss", cancelled: "draw", waiting: "warn"}[s] || "";
  return `<span class="chip ${cls}">${esc(s)}</span>`;
};
const table = (head, rows) => `<table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>`
  + `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c ?? ""}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
"""

LIST = """<h1>Duels <span class="sub">ad-hoc A vs B on fresh seeds, off the ladder (docs/DUELS.md)</span></h1>
<p class="sub">Start one with <code>tokenz duel A B --seeds random:8 --wait</code> (an unsubmitted bot:
<code>tokenz ephemeral ./bot</code> first), or the MCP tools <code>ephemeral</code>, <code>duel</code> and
<code>duel_result</code>.</p>
<div id="duels">loading…</div>
<h2>Ephemeral bots <span class="sub">duels only; removed after their TTL</span></h2>
<div id="eph"></div>
<script>""" + JS_COMMON + r"""
async function load() {
  const [ds, es] = await Promise.all([fetch("/api/v1/duels?limit=200").then((r) => r.json()),
                                      fetch("/api/v1/ephemeral").then((r) => r.json())]);
  document.getElementById("duels").innerHTML = ds.length ? table(
    ["duel", "A", "B", "status", "played", "owner", "created", "note"],
    ds.map((d) => [`<a href="/duels/${d.id}">${d.id}</a>`, bot(d.a), bot(d.b), status(d.status),
                   `${d.games_planned - d.games_left}/${d.games_planned}`, esc(d.owner), esc(d.created), esc(d.note)]))
    : "<p class='muted'>No duels yet.</p>";
  document.getElementById("eph").innerHTML = es.length ? table(
    ["id", "name", "status", "owner", "expires", "reason"],
    es.map((x) => [esc(x.id), esc(x.name), status(x.status), esc(x.owner), esc(x.expires), esc(x.reason)]))
    : "<p class='muted'>None.</p>";
}
load(); setInterval(load, 15000);
</script>"""

DETAIL = """<h1 id="title">Duel {did}</h1>
<p class="sub" id="meta">loading…</p>
<div class="grid2">
<div><h2>Record <span class="sub">from A's side</span></h2><div id="record"></div>
<h2>By suite</h2><div id="by_suite"></div>
<h2>How games ended</h2><div id="by_end"></div>
<h2>Starting dragons per side</h2><div id="by_starters"></div></div>
<div><h2>Metrics <span class="sub">each bot from its own seat; mean over games</span></h2><div id="sides"></div>
<h2>Queens, deaths, CPU</h2><div id="queens"></div></div>
</div>
<h2>By map <span class="sub">worst for A first</span></h2><div id="by_map"></div>
<h2>Games</h2><div id="games"></div>
<script>""" + JS_COMMON + r"""
const did = __DID__;
const FINAL = ["done", "cancelled", "failed"];
const KEYS = ["dragons", "total_length", "longest", "board_control", "pearl_rate", "queen_alive", "queen_length", "tiles_visited", "tiles_seen"];
const ROUNDS = ["100", "200", "300", "400", "end"];
async function load() {
  const r = await fetch(`/api/v1/duels/${did}`);
  if (!r.ok) { document.getElementById("meta").textContent = "no such duel"; return; }
  const d = await r.json();
  document.getElementById("title").innerHTML = `${bot(d.a)} vs ${bot(d.b)} <span class="sub">${esc(d.id)}</span>`;
  document.getElementById("meta").innerHTML = `${status(d.status)} · ${d.games_played}/${d.games_planned} played`
    + (d.games_running ? `, ${d.games_running} running` : "") + (d.errors ? `, <b class="L">${d.errors} errors</b>` : "")
    + ` · seeds ${esc(d.seeds)} · suites ${esc(d.suites.join(", "))} · ${d.both_seats ? "both seats" : "one seat"}`
    + ` · by ${esc(d.owner)}` + (d.note ? ` · ${esc(d.note)}` : "") + (d.reason ? ` · ${esc(d.reason)}` : "");
  const rec = d.record;
  document.getElementById("record").innerHTML = `<p style="font-size:20px">${wdl(rec)} · ${score(rec)}</p>`
    + `<p class="sub">${!rec.games ? "no games yet" : rec.significant ? "the 95% interval excludes 50%: a real difference"
                                                     : "the 95% interval includes 50%: within noise"}</p>`;
  const grouped = (o) => Object.keys(o || {}).length ? table(["", "W-D-L", "score (95% CI)"],
    Object.entries(o).map(([k, v]) => [esc(k), wdl(v), score(v)])) : "<p class='muted'>-</p>";
  for (const k of ["by_suite", "by_end", "by_starters"]) document.getElementById(k).innerHTML = grouped(d[k]);
  const maps = Object.entries(d.by_map || {}).sort((x, y) => (x[1].score ?? 50) - (y[1].score ?? 50));
  document.getElementById("by_map").innerHTML = maps.length ? table(["map", "W-D-L", "score (95% CI)"],
    maps.map(([k, v]) => [esc(k), wdl(v), score(v)])) : "<p class='muted'>-</p>";
  const sa = d.sides.a, sb = d.sides.b;
  const rows = [];
  for (const k of KEYS) for (const rnd of ROUNDS) {
    const key = `${k}@${rnd}`, x = sa.metrics[key], y = sb.metrics[key];
    if (x == null && y == null) continue;
    const better = x == null || y == null || x === y ? "" : (x > y ? "a" : "b");
    rows.push([esc(k), rnd === "end" ? "end" : `r${rnd}`, `<span class="${better === "a" ? "W" : ""}">${x ?? "-"}</span>`,
               `<span class="${better === "b" ? "W" : ""}">${y ?? "-"}</span>`]);
  }
  document.getElementById("sides").innerHTML = rows.length ? table(["metric", "round", esc(d.a), esc(d.b)], rows)
    : "<p class='muted'>No metrics yet.</p>";
  const causes = (q) => Object.entries(q.causes || {}).map(([c, n]) => `${esc(c)} ${n}`).join(", ") || "-";
  const deaths = (s) => Object.entries(s.deaths || {}).slice(0, 4).map(([c, n]) => `${esc(c)} ${n}`).join(", ") || "-";
  const cpu = (s) => s.cpu.max == null ? "-" : `${(s.cpu.max / 1e6).toFixed(1)}M max` + (s.cpu.overruns ? `, <b class="L">${s.cpu.overruns} over the cap</b>` : "");
  document.getElementById("queens").innerHTML = table(["", esc(d.a), esc(d.b)], [
    ["queen alive at end", sa.queen.alive_at_end_pct == null ? "-" : `${sa.queen.alive_at_end_pct}%`,
     sb.queen.alive_at_end_pct == null ? "-" : `${sb.queen.alive_at_end_pct}%`],
    ["median queen death", sa.queen.median_death_round ?? "-", sb.queen.median_death_round ?? "-"],
    ["queen deaths by", causes(sa.queen), causes(sb.queen)],
    ["dragon deaths", deaths(sa), deaths(sb)], ["CPU per turn", cpu(sa), cpu(sb)]]);
  document.getElementById("games").innerHTML = (d.games || []).length ? table(
    ["#", "map", "seed", "seat A", "seat B", `for ${esc(d.a)}`, "rounds", "how", "judge", ""],
    d.games.map((g) => [g.n, esc(`${g.suite}/${g.map}`), `<span class="sub">${g.seed}</span>`, bot(g.seat_a), bot(g.seat_b),
      g.error ? `<b class="L" title="${esc(g.error)}">error</b>` : `<span class="${g.result_for_a}">${g.result_for_a}</span>`,
      g.rounds ?? "", esc(g.reason || ""), esc(g.judge || ""), g.watch ? `<a href="${g.watch}">watch</a>` : ""]))
    : "<p class='muted'>No games played yet.</p>";
  if (!FINAL.includes(d.status)) setTimeout(load, 10000);
}
load();
</script>"""


def make_router(arena) -> APIRouter:
    from arena.web.pages import layout
    r = APIRouter()

    @r.get("/duels")
    def duels_page():
        return layout("Duels", LIST, ["/api/v1/duels", "/api/v1/ephemeral"], "/duels")

    @r.get("/duels/{did}")
    def duel_page(did: str):
        import json
        import re
        from fastapi import HTTPException
        if not re.fullmatch(r"duel-[a-z0-9]{1,32}", did):     # it goes into the page's script
            raise HTTPException(404, "no such duel")
        return layout(f"Duel {did}", DETAIL.replace("{did}", e(did)).replace("__DID__", json.dumps(did)),
                      f"/api/v1/duels/{did}", "/duels")

    return r
