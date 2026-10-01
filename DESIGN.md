# tokenz-arena: design

An always-on arena on poscatgpu2 for the team. People and their LLM agents upload unswbc
bots as zips over HTTP. The arena plays every submission against the others with the
official judge, and serves ratings, diagnostics, stats, replays and every submission's
source back over the same HTTP API (and MCP). It's the shared league that every teammate's
agent loop measures against.

## Principles

- **The official engine is the only judge.** Every game is `unswbc run <map> A B --sandbox
  --seed S`, at the toolkit version the competition server runs (pinned in
  `pyproject.toml`). There are no ports and no fast sims, so a result here means what it
  means on the server.
- **One HTTP API does everything.** Submit a zip, poll or wait for its verdict, read
  reports, download replays and sources. An agent needs nothing but `curl` (or the MCP
  tools), and there's no GitHub, CI or token plumbing between an idea and its result.
- **A submission is an experiment.** It carries its parents and a note (the hypothesis and
  what changed). The arena pairs that note with measured results, so the submission history
  is the team's experiment log.
- **Submissions are immutable.** A changed bot is a new submission. Old ones keep their
  games, and their sources stay downloadable, so anyone can build on anyone's bot.
- **Feedback in minutes.** Agent loops are only as fast as their judge. A draft gets a
  verdict against its parent and the top bots within about 5 minutes. Thorough ranking runs
  afterwards, in the background.
- **Diagnose, don't just rank.** Every submission gets a report of where and why it loses.
  An agent with only an Elo number can't improve a bot.
- **No database.** State is plain JSON files, one per submission and one per game, written
  once with an atomic rename. You can read them with `cat` and back them up with `rsync`.
  One process (API, scheduler and judge workers) owns them. Restarting it re-plays only the
  games that were running.

## How the team iterates (the loop this arena serves)

Lessons from ~26 generations with 6 parallel agents in `syeyoung-tokens` (its
`ORCHESTRATOR.md` and `league/KNOWLEDGE.md`):

1. **The judge's latency sets the loop's speed.** About 20-minute tasks, rated 3 minutes
   later, worked well. Waiting hours for a ranking doesn't.
2. **Child vs parent head-to-head is the cleanest signal.** Elo deltas inside the error bars
   misled us. So every submission names its parent, and the arena plays that pair first.
3. **Small additions to a strong base beat rewrites.** From-scratch lines won locally but
   dropped behaviours the strong line had built up, and lost on the server. So sources are
   shared and lineage is visible, which makes building on the best bot the easy path.
4. **Single views narrow the search.** One person's agents converge on one idea. Many
   people's agents sharing one judge and one experiment log is this arena's advantage.
5. **Local wins aren't server wins.** A pool tuned against itself overfits. Hidden maps, a
   diverse opponent pool and imported server results counter that.

**A teammate's agent loop with the arena:**

```
GET  /api/v1/leaderboard                 pick a strong base
GET  /api/v1/submissions/{id}/source     download it
GET  /api/v1/submissions/{id}/report     where it loses
GET  /api/v1/ideas?q=<topic>             what's been tried, with results
     ...change one thing...
POST /api/v1/submissions                 zip + {bot, parent, note, lane: "draft"}
GET  /api/v1/submissions/{id}?wait=600   blocks until the draft verdict
     beats its parent?  ->  POST /api/v1/submissions/{id}/promote   (joins the leaderboard)
     loses?             ->  the note and the numbers stay in /ideas for the next agent
```

Each teammate can still run an orchestrator (strategist subagents propose, workers
implement one idea each). The arena replaces the local league and makes the results
shared.

## Components

```
POST zip --> api --> checks --> submissions/<id>/{meta.json, source.zip}
                                     |
                        scheduler: draft > placement > holdout > continuous
                                     |
                worker pool: unswbc run --sandbox -v (N at a time)
                                     |
             games/<id>.json (verdict + stats) + replays/<id>.replay
                                     |
               rating (Bradley-Terry refit) + reports
                                     |
     api: JSON, replay and source downloads, /llms.txt, MCP at /mcp
```

| Module | Job |
|---|---|
| `arena/config.py` | Loads `arena.toml`: paths, workers, map pools, seeds, tokens file. |
| `arena/store.py` | JSON files, one per record, atomic writes; everything is loaded into memory on start. |
| `arena/submit.py` | Validates an uploaded zip, freezes it, runs the checks. |
| `arena/runner.py` | Runs one game with `unswbc run --sandbox -v` and parses verdict, deaths and CPU points. |
| `arena/stats.py` | Per-game stats from the verbose log: lengths and dragon counts over rounds, deaths by cause, tiebreak numbers. |
| `arena/scheduler.py` | Picks the next games: drafts, placement, holdout, continuous; fair share per owner. |
| `arena/rating.py` | Bradley-Terry ratings over ranked games among current submissions. |
| `arena/report.py` | A submission's diagnostic report (by opponent, by map, deaths, CPU, worst-loss replays). |
| `arena/daemon.py` | The background part of `arena serve`: schedule, play, rate. |
| `arena/api/http.py` | The HTTP API (the one write is submitting) and `/llms.txt`. |
| `arena/api/mcp.py` | The same as MCP tools. |
| `arena/cli.py` | `arena serve`, `arena status`, `arena play`, `arena token`, `arena import-server`. |

## 1. Submitting

```
POST /api/v1/submissions          Authorization: Bearer <teammate token>
  multipart: file = bot.zip       an unswbc project: bot.toml at the root (or one folder down)
             bot = "swarm"        the bot's name; its submissions form a line
             parent = "<id>"      optional: the submission this builds on (anyone's)
             note = "..."         the hypothesis and what changed (markdown)
             lane = "draft"       draft (default) | ranked
-> 202 {"id": "swarm-7", "status": "checking", "url": "/api/v1/submissions/swarm-7"}
```

- **Names:** a bot belongs to the teammate who first submitted it. Submission ids are
  `<bot>-<n>`.
- **Lanes:**
  - A **draft** plays its parent and the current top 3 on the public maps, both seats,
    unranked.
  - **Promote** a draft (`POST .../promote`), or submit with `lane = "ranked"`, and it gets
    full placement and joins the leaderboard as its bot's current submission.
  - Agents iterate on drafts and promote only what beats its parent.
- **Auth:** one bearer token per teammate (`arena token add <name>`), stored hashed. Tokens
  are needed only to submit and promote. Reads are open on the tailnet.
- **Checks** (a submission that fails is `rejected`, with the reason and the judge's log in
  the API):
  1. The zip is safe: size limit, no absolute paths or `..`, no links. `bot.toml` parses, and
     the language is c, cpp or python.
  2. Competition rules: no environment variable reads, and the upload size limit.
  3. It builds and survives: one sandboxed game against the reference bot on the smallest map.
- **Duplicates:** a zip whose content hash matches an existing submission returns that one
  instead of playing again.

## 2. Playing games

- **Engine:** `unswbc run <map> <A> <B> --sandbox --seed <S> -v -o <replay dir>`. The
  verdict is the line `team A|B wins after R rounds (by <reason>)` or a draw. Deaths come
  from `round R: bot N (team X) died: <cause>`, CPU from the `points per turn` lines. Each
  game runs on fresh copies of both bots, so parallel games never share build output.
- **Cost:** a trivial Python vs Python game took 6 s and about 3 cores on a Mac (68 rounds).
  Real bots going 500 rounds cost more, and the first job on poscatgpu2 is to measure that
  with our strongest C++ bot. The worker count and the draft size follow from that number.
  The 28 cores should allow 6 to 9 parallel games.
- **Maps:** `maps/official/` (the official maps) and `maps/rotating/` (worldgen, refreshed
  weekly, public). The holdout pool lives only in the data directory. Each game records the
  map's suite, name and hash.
- **Seeds:** both seats on the same map and seed. Seeds come from a fixed list per suite,
  so results stay comparable over time.

## 3. Scheduling

Priority order, each level only when the one above is idle:

1. **Drafts:** parent and top 3, public maps, both seats. Target: verdict within 5 minutes.
2. **Placement:** a new ranked submission plays its parent, then the current submission of
   every other active bot (official maps, both seats). It shows as `placing` until done.
3. **Holdout:** the top 4 ranked on the hidden maps.
4. **Continuous:** current submissions against each other, preferring the fewest games and
   the closest ratings.

When drafts pile up, owners share the workers fairly, so one teammate's 20 agents can't
starve the others. Each owner also has a cap on queued drafts, and submitting past it
returns 429.

## 4. Ratings

Bradley-Terry over ranked games among current submissions (a draw counts as half a win). It's
refit after each batch and reported on an Elo-like scale with a confidence interval, with a
fixed reference bot at 1000. Head-to-head tables sit beside the number, because one number
hides rock-paper-scissors.

## 5. Stats, replays and sources

- **Per game (stored):** seats, map, seed, winner, rounds, end reason, tiebreak numbers
  (queen, longest, total), deaths by cause per side, kills, CPU points per turn (p50, p99,
  max), and the dragon count and total length per side every 10 rounds.
- **Per submission (computed):** win % by opponent and by map, death profile, CPU headroom
  against the 100M cap, child vs parent, rating history.
- **Replays:** every game's `.replay` file, downloadable, which opens in the unswbc VS Code
  viewer. Kept for current submissions, drafts from the last 3 days and everything from the
  last 14 days. Verdicts and stats are kept forever.
- **Sources:** every submission's zip, kept forever (they're small).
- **Disk:** poscatgpu2's root volume is 94% full (123 GB free). Retention plus a hard cap
  (`replay_cap_gb`) keeps replays bounded.

## 6. The API

JSON under `/api/v1`, small flat responses, and `/llms.txt` explaining everything on one
page for agents.

| Endpoint | Returns |
|---|---|
| `POST /api/v1/submissions` | Submit a zip (section 1). Needs a token. |
| `POST /api/v1/submissions/{id}/promote` | Draft to ranked. The owner's token. |
| `GET /api/v1/submissions/{id}` | Status, checks, rejection reason, parent, note, draft verdict. `?wait=S` blocks until the verdict is in. |
| `GET /api/v1/submissions/{id}/report` | The diagnostic report (section 5). |
| `GET /api/v1/submissions/{id}/source` | The zip. |
| `GET /api/v1/submissions?bot=&owner=&lane=&status=` | Submissions, newest first. |
| `GET /api/v1/leaderboard` | Current ranked submissions: rating ±, games, win %, status, holdout W-D-L (top 4). |
| `GET /api/v1/bots/{bot}` | A bot's line: submissions, lineage, rating history, head-to-head table. |
| `GET /api/v1/ideas?q=&bot=&outcome=` | Search notes, paired with their verdicts (beat parent or not, promoted). |
| `GET /api/v1/games?sub=&vs=&map=&suite=&result=&limit=` | Games with verdicts and stats. |
| `GET /api/v1/games/{id}` | One game in full, with the stats series. |
| `GET /api/v1/games/{id}/replay` | The replay file. |
| `GET /api/v1/stats/maps` | Per map: games, how they end, first-seat advantage. |
| `GET /api/v1/queue` | What's playing and waiting, by lane and owner. |
| `GET /health` | Daemon heartbeat, workers, unswbc version, disk free. |
| `GET /llms.txt` | A plain-text guide to all of the above. |

**MCP** (`/mcp`, streamable HTTP) wraps the same endpoints as tools: `submit` (takes a
directory path or base64 zip), `submission`, `wait`, `promote`, `report`, `leaderboard`,
`ideas`, `games`, `game`, `map_stats`, `queue`. A small HTML page for people (leaderboard,
bot pages, replay links) comes after the JSON.

## 7. Deployment on poscatgpu2

- Python 3.12 with `uv`. unswbc goes in the project venv at the competition's version.
- One systemd user service (`deploy/arena.service`, running `arena serve`). Run
  `loginctl enable-linger` so it survives logout.
- State lives in `~/tokenz-arena-data/`: `submissions/`, `games/`, `replays/`, `work/`,
  `maps/holdout/`, `tokens.json`, `ratings.json`, `logs/`. Back it up with `rsync`.
- Network: binds to the tailnet address, for teammates on Tailscale.

## Build order

1. Runner and stats: one game end to end, parsed (`arena play <map> <dirA> <dirB>`).
   Measure a real 500-round game's cost on poscatgpu2.
2. The JSON store, `POST /submissions` with checks, `GET /submissions/{id}`.
3. Scheduler and worker pool: drafts and placement; `?wait=`.
4. Ratings, reports, leaderboard, `arena status`.
5. Replay and source downloads, games, `/ideas`, `/llms.txt`.
6. MCP.
7. Deploy on poscatgpu2, tokens for teammates; seed it with our best bots.
8. Holdout, continuous matches, server import, retention, the HTML page.
9. `docs/AGENTS.md`: the brief for teammates' agents (the loop above, with curl examples).

## Open decisions

- The draft size (parent + top 3 × official maps × 2 seats = 120 games) depends on the
  measured cost of a game.
- Should `promote` require beating the parent, or is it the owner's call?
- Should our syeyoung-tokens variants enter through `make-bot.sh` exports, as one bot each?
