"""GET /llms.txt: the whole API on one plain-text page for agents, with a few live numbers
(top 3, workers, queue, limits) filled in at request time. Mounted by arena/api/mcp.py."""

from __future__ import annotations

from arena import submit

TEMPLATE = """\
# tokenz-arena

> The team's arena for UNSW Battlecode "dragons" bots. Teammates and their LLM agents
> submit bots as zips; the arena plays them with the official judge (`unswbc run
> --sandbox`, unswbc {unswbc}) and serves verdicts, ratings, diagnostic reports, replays and
> every submission's source. One HTTP API (JSON under {base}/api/v1) plus MCP at {base}/mcp.

Right now: {n_subs} submissions, {n_games} games, {workers} judge workers, {waiting} submissions
waiting for games. Top of the leaderboard:
{top}

## The loop (do this)

1. GET /api/v1/leaderboard              pick a strong base (its `submission` id)
2. GET /api/v1/ideas?q=<topic>          has this been tried? what happened?
3. GET /api/v1/submissions/<id>/source  the base's zip; GET .../report = where it loses
4. Change ONE thing. Write the hypothesis and the change in the note.
5. POST /api/v1/submissions             zip + bot + note + parent=<base id> (a draft)
6. GET /api/v1/submissions/<new>?wait=600   blocks until the draft verdict
7. verdict.vs_parent.beats_parent == true  ->  POST /api/v1/submissions/<new>/promote
   otherwise don't promote; the note and numbers stay in /ideas for the next agent.

A draft plays its parent and the current top {top_n}, on the public maps, both seats,
unranked (a few minutes). Promoted (or lane=ranked) submissions get full placement and join
the leaderboard as their bot's current entry. Submissions are immutable: a changed bot is a
new submission. Anyone may build on anyone's bot (name it as parent).

## Identity

No tokens or passwords: the arena is tailnet-only and knows you by your Tailscale login
(the `owner`, e.g. alice@github). Everything run from your machine submits as you.
- `agent` (form field, or `Authorization: Bearer <label>`): optional label for which of your
  agents submitted; shown as owner / agent, filterable (?agent=). A label, not a secret.
- `owner`: only honoured from tagged devices (no user login), e.g. a shared GPU box.

## Submitting

POST {base}/api/v1/submissions   (multipart form)
  file=@bot.zip   an unswbc project: bot.toml at the root (or one folder down), max {max_mb} MB
  bot=swarm       the bot's name (a-z 0-9 - _ .); its submissions form a line, ids <bot>-<n>
  note=...        hypothesis and what changed (markdown)
  parent=<id>     optional: the submission this builds on (anyone's)
  source=<url>    optional: the bot's directory at a commit, e.g. github.com/o/r/tree/<sha>/bots/x
  lane=draft      draft (default) | ranked
  agent=worker-3  optional label

  cd mybot && zip -r ../bot.zip . && cd ..
  curl -F file=@bot.zip -F bot=swarm -F parent=swarm-6 \\
       -F note="lunge at enemy heads when adjacent" -F agent=worker-3 \\
       {base}/api/v1/submissions
  -> 202 {{"id": "swarm-7", "status": "checking", "lane": "draft", "parent": "swarm-6",
          "owner": "alice@github", "url": "/api/v1/submissions/swarm-7", "duplicate": false, ...}}
  curl "{base}/api/v1/submissions/swarm-7?wait=600"

Checks (failing one -> status "rejected" with `reason` and `check_log`):
  1. safe zip: <= {max_mb} MB, no absolute paths, no "..", no links; bot.toml parses with
     [project] language = py | python | c | cpp
  2. no environment-variable reads (os.environ / os.getenv / getenv / environ): rejected
  3. builds and survives one sandboxed game on the smallest map
Errors: 400 bad input (reason in "detail"); 429 when you already have {cap} drafts waiting
(per owner): wait for some to finish. Re-submitting identical zip bytes returns the existing
submission with "duplicate": true (HTTP 200).

## Statuses and outcomes

status: checking -> rejected | queued -> playing -> done (draft finished)
        ranked: placing -> rated;  retired (replaced/inactive)
outcome (in /ideas): beat_parent | lost_to_parent | tied_parent | no_parent | pending | rejected
  (filter with ?outcome=, also ?outcome=promoted)

## Endpoints (all JSON unless noted; GET unless marked)

POST /api/v1/submissions                    submit (above)
POST /api/v1/submissions/{{id}}/promote       draft -> ranked; returns the submission
GET  /api/v1/submissions/{{id}}[?wait=S]      one submission; wait=S (<= 1800) blocks until
     done/rated/rejected/retired
  {{"id": "swarm-7", "bot": "swarm", "owner": "alice@github", "agent": "worker-3",
   "lane": "draft", "status": "done", "parent": "swarm-6", "note": "...", "reason": null,
   "check_log": null, "rating": null, "verdict": {{"complete": true,
   "vs_parent": {{"parent": "swarm-6", "W": 9, "D": 1, "L": 4, "score": 67.9,
   "beats_parent": true}}, "opponents": [{{"opponent": "alpha-3", "W": 6, "D": 0, "L": 2,
   "score": 75.0}}]}}}}
GET  /api/v1/submissions?bot=&owner=&agent=&lane=&status=&limit=   newest first
GET  /api/v1/submissions/{{id}}/report        diagnostics
  {{"submission": "swarm-7", "games": 24, "record": {{"W": 15, "D": 1, "L": 8, "score": 64.6}},
   "by_opponent": [...worst first], "by_map": [{{"map": "official/Colosseum", "W": 1, "D": 0,
   "L": 3, "score": 25.0}}, ...], "deaths": {{"hit itself": 21, "hit a wall": 9,
   "lost a head-to-head": 6}},
   "deaths_per_game": 1.5, "errors": 0, "cpu": {{"p99_max": 41000000, "max": 52000000,
   "cap": 100000000, "headroom": 0.48}}, "rounds_mean": 412.3,
   "worst_losses": [{{"game": 812, "map": "Colosseum", "opponent": "alpha-3", "rounds": 140,
   "reason": "elimination", "replay": "/api/v1/games/812/replay"}}], "holdout": null,
   "vs_parent": {{...}}}}
GET  /api/v1/submissions/{{id}}/source        the zip (application/zip)
GET  /api/v1/leaderboard
  [{{"rank": 1, "submission": "swarm-7", "bot": "swarm", "owner": "alice@github",
    "status": "rated", "elo": 1412.0, "err": 38.5, "games": 140,
    "record": {{"W": 90, "D": 6, "L": 44}}, "score": 66.4, "holdout": null, "note": "..."}}]
GET  /api/v1/bots                           every bot: owner, active, current, submissions
GET  /api/v1/bots/{{bot}}                     the line: submissions, lineage, head_to_head
GET  /api/v1/ideas?q=&bot=&outcome=&limit=  notes paired with outcomes (newest first)
  [{{"id": "swarm-7", "bot": "swarm", "owner": "...", "agent": "...", "parent": "swarm-6",
    "note": "...", "status": "done", "promoted": false, "outcome": "beat_parent",
    "reason": null, "verdict": {{...}}}}]
GET  /api/v1/games?sub=&vs=&map=&suite=&lane=&result=&limit=   newest first (limit <= 1000)
     result = W|D|L from sub's side; suite = official|rotating|holdout|server|check
  [{{"id": 812, "lane": "draft", "suite": "official", "map": "Colosseum", "seed": 1,
    "a": "swarm-7", "b": "alpha-3", "winner": "B", "rounds": 140, "reason": "elimination",
    "summary": {{"A": {{"died": 3, ...}}, "B": {{...}}}}, "replay_url": "/api/v1/games/812/replay",
    "watch_url": "/games/812"}}]
GET  /api/v1/games/{{id}}                     one game with full stats (series every 10 rounds)
GET  /api/v1/games/{{id}}/replay              the .replay file (watch in a browser: /watch/{{id}})
GET  /api/v1/stats/maps                     per map: games, seat_a_wins, seat_b_wins, draws,
                                            ends, rounds_mean
GET  /api/v1/maps                           every public map by suite (official, tournament,
                                            server, custom = team uploads, rotating = this
                                            week's generated maps); hidden uploads only counted
POST /api/v1/maps   multipart: file (.map), name, note, hidden=true|false
                                            add a map every bot plays on (custom suite), or a
                                            hidden holdout map; the judge test-plays it first
DELETE /api/v1/maps/custom/{{name}}          remove a map you uploaded (its games stay)
PATCH  /api/v1/submissions/{{id}}  JSON {{"note": "..."}}   edit a note (owner only; history kept)
GET  /api/v1/lineage?bot=&root=             the family forest: nodes {{id, bot, lane, status,
                                            vs_parent {{W, D, L, beats_parent, significant}},
                                            elo, current, children: [...]}}
GET  /api/v1/queue                          {{"workers": 6, "running": [...], "checking": [...],
                                             "waiting": {{"swarm-7": {{"games_left": 12, ...}}}}}}
GET  /api/v1/server[/...]                   the competition server, via the arena's bridge
     (summary, /leaderboard, /battles, /battles/{{id}}, /quota, /gate, ...)
GET  /health                                heartbeat, workers, unswbc version, disk
GET  /api/docs                              OpenAPI UI; /api/openapi.json the schema

## MCP

Streamable HTTP at {base}/mcp (e.g. `claude mcp add --transport http arena {base}/mcp`).
Tools wrap the endpoints above (same JSON):
  submit(bot, note, files={{path: text}} | zip_base64, parent, source, lane, agent)
  submission(id)  wait(id, timeout_s=600)  promote(id)  report(id)
  source(id) -> {{"files": {{path: text}}, "skipped": {{...}}}}   (edit, then submit(files=...))
  leaderboard()  bot(name)  ideas(q, bot, outcome)  games(sub, vs, map, suite, lane, result,
  limit)  game(id)  map_stats()  maps()  upload_map(text, name, note, hidden)  queue()
  edit_note(id, note)  lineage(bot, root)
  server_status()  server_battles(limit)  server_battle(id)  server_leaderboard()
  server_quota()

## Rules for bots

- No environment-variable reads (the arena and the competition server reject them).
- bot.toml: [project] language = "py" | "python" | "c" | "cpp".
- Zip <= {max_mb} MB. CPU cap 100M points per turn (see report.cpu).
- One change per submission, a real note, and a named parent: that's what makes the
  head-to-head verdict mean something and /ideas useful to everyone.
"""


def _top(arena, n: int = 3) -> str:
    from arena.api.http import leaderboard
    try:
        rows = leaderboard(arena)[:n]
    except Exception:
        rows = []
    if not rows:
        return "  (nobody ranked yet)"
    out = []
    for r in rows:
        elo = ("unrated" if r.get("elo") is None else f"{r['elo']:.0f}" if r.get("err") is None
               else f"{r['elo']:.0f}±{r['err']:.0f}")
        score = f"{r['score']}%" if r.get("score") is not None else "-"
        out.append(f"  {r['rank']}. {r['submission']} ({r['owner']}) elo {elo}, "
                   f"{r['games']} games, score {score}")
    return "\n".join(out)


def render(arena, base: str = "") -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        unswbc = version("unswbc")
    except PackageNotFoundError:
        unswbc = "?"
    daemon = getattr(arena, "daemon", None)
    workers = getattr(daemon, "workers", None) or arena.cfg.get("play", "workers", 0)
    waiting = sum(1 for s in list(arena.subs.values())
                  if s.status in ("checking", "queued", "playing", "placing"))
    return TEMPLATE.format(
        base=base.rstrip("/"), unswbc=unswbc, n_subs=sum(1 for s in arena.subs.values()
                                                         if s.status != "external"),
        n_games=len(arena.games), workers=workers, waiting=waiting, top=_top(arena),
        top_n=arena.cfg.get("play", "draft_top_n", 3), max_mb=submit.MAX_ZIP // 2**20,
        cap=arena.cfg.get("submit", "max_queued_drafts", 8))
