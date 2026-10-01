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
  tools), and there's no GitHub, CI or account plumbing between an idea and its result.
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
| `arena/config.py` | Loads `arena.toml`: paths, workers, map pools, seeds. |
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
| `arena/server/client.py` | The competition server: the key API, plus the `bcweb` package (`Battlecode-UnderTheC/bcweb`) for what only the website has (quota, per-map detail, brackets); cached. |
| `arena/server/bridge.py` | Auto-submit behind the champion gate, auto-download of battles and replays, the server suite (section 8). |
| `arena/who.py` | Who's calling: the teammate's tailnet login, from Tailscale (section 1). |
| `arena/cli.py` | `arena serve`, `arena status`, `arena play`, `arena server <status\|pause\|resume>`. |

## 1. Submitting

```
POST /api/v1/submissions          (no auth: the caller is known from Tailscale)
  multipart: file = bot.zip       an unswbc project: bot.toml at the root (or one folder down)
             bot = "swarm"        the bot's name; its submissions form a line
             parent = "<id>"      optional: the submission this builds on (anyone's)
             note = "..."         the hypothesis and what changed (markdown)
             source = "<url>"     optional: the bot's directory at a commit on GitHub
             lane = "draft"       draft (default) | ranked
             agent = "worker-3"   optional label: which of the teammate's agents
             owner = "<name>"     only from tagged devices without a Tailscale user login
-> 202 {"id": "swarm-7", "status": "checking", "url": "/api/v1/submissions/swarm-7"}
```

- **Source link:** optional provenance, shown on the submission's page with the note. The
  zip is still what's judged and stored, so the arena never fetches the link.
  - **Convention:** one directory per bot in a repo, no branch per bot. The link is that
    directory at the commit the zip came from:
    `https://github.com/<org>/<repo>/tree/<sha>/bots/<name>`. A commit link never moves, so it
    always matches the zip.
  - A link to a branch (`/tree/main/...`) is accepted but flagged "moving" on the page.
  - A small `arena-submit <dir>` helper (later, in this repo's `scripts/`) zips the
    directory and fills in `source` itself from `git remote`, `git rev-parse HEAD` and the
    directory's path. It warns when the directory has uncommitted changes, because the link
    would then not match the zip.
- **Names:** a bot belongs to the teammate who first submitted it. Submission ids are
  `<bot>-<n>`.
- **Lanes:**
  - A **draft** plays its parent and the current top 3 on the public maps, both seats,
    unranked.
  - **Promote** a draft (`POST .../promote`), or submit with `lane = "ranked"`, and it gets
    full placement and joins the leaderboard as its bot's current submission.
  - Agents iterate on drafts and promote only what beats its parent.
- **Who's who (no auth):** the arena is reachable only on the tailnet, so there are no
  tokens or passwords. Tailscale already knows who is calling, and the arena asks it:
  - Behind `tailscale serve`, each request carries `Tailscale-User-Login` (e.g.
    `alice@github`) and `Tailscale-User-Name`.
  - Direct connections are looked up with the local Tailscale API (`tailscale whois <ip>`).

  The login becomes the submission's `owner`. Teammates and their agents need no setup:
  whatever runs on Alice's laptop submits as Alice.
  - **Agent label:** an optional `agent` field labels which of a teammate's agents
    submitted. It shows next to the owner (`alice / worker-3`) and lets a teammate
    filter their own loop's submissions. It's a label, not a secret.
  - **Shared machines:** tagged devices (a server running someone's agents, poscatgpu2
    itself) have no user login. Their submissions are owned by the device name (`tag:ci /
    gpu-box`) unless they pass `owner`. Only devices without a user login may pass `owner`.
  - Fair share and the per-owner draft cap (section 3) use this owner.
  - It's only enough to tell teammates apart. Anyone on the tailnet can do anything the API
    allows, which is fine for a team.
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
- **Replays:** every game's `.replay` file, downloadable, and watchable in the browser (see
  section 7). Kept for current submissions, drafts from the last 3 days and everything from the
  last 14 days. Verdicts and stats are kept forever.
- **Sources:** every submission's zip, kept forever (they're small).
- **Disk:** poscatgpu2's root volume is 94% full (123 GB free). Retention plus a hard cap
  (`replay_cap_gb`) keeps replays bounded.

## 6. The API

JSON under `/api/v1`, small flat responses, and `/llms.txt` explaining everything on one
page for agents.

| Endpoint | Returns |
|---|---|
| `POST /api/v1/submissions` | Submit a zip (section 1). |
| `POST /api/v1/submissions/{id}/promote` | Draft to ranked. Recorded with who promoted it. |
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
| `GET /api/v1/server/*` | The competition server: summary, ladder, our submissions and battles, challenges, quota, maps, tournaments (section 8). |
| `GET /health` | Daemon heartbeat, workers, unswbc version, disk free. |
| `GET /llms.txt` | A plain-text guide to all of the above. |

**MCP** (`/mcp`, streamable HTTP) wraps the same endpoints as tools: `submit` (takes a
directory path or base64 zip), `submission`, `wait`, `promote`, `report`, `leaderboard`,
`ideas`, `games`, `game`, `map_stats`, `queue`, and `server_*` tools for section 8
(`server_status`, `server_battles`, `server_battle`, `server_leaderboard`, `server_team`,
`server_quota`, `server_challenge`, `server_tournament`).

## 7. The website (for people)

Server-rendered HTML from the same process and the same in-memory state as the JSON API.
No frontend build, no JavaScript framework. Each page has a JSON twin, so a person and an
agent always see the same numbers.

| Page | Shows |
|---|---|
| `/` | Leaderboard: rating ±, win %, games, owner, status. A "recent" strip of new submissions and their draft verdicts. |
| `/bots/{bot}` | The bot's line: submissions in order with notes and verdicts, a lineage tree across bots, a head-to-head grid, rating over time. |
| `/s/{id}` | One submission: its note, its checks, the draft verdict vs its parent and the top bots, the report (worst maps, death profile, CPU), source download, and a list of games, each with a "watch" link. |
| `/games/{id}` | The replay viewer, full window, with the game's verdict and seats above it. |
| `/games?…` | Filterable game list (bot, opponent, map, result). |
| `/maps` | Per-map stats, with a preview of each map. |
| `/ideas` | Searchable notes with outcomes: the experiment log. |
| `/submit` | Upload form (zip, bot, parent, note, source) for people who aren't using curl. Shows "submitting as alice@github". |
| `/queue` | What's playing now and what's waiting. |
| `/server` | The competition server: live bot, rank, battles with replays, quota, the candidate and why. |

**Replay viewer in the browser.** The toolkit ships its VS Code replay viewer
(`unswbc/replay-viewer.vsix`). The webview inside it is a self-contained bundle whose only
tie to VS Code is `acquireVsCodeApi().postMessage({type: "ready"})`, answered with
`{type: "open", name, url}`. `arena/web/viewer.py` serves it in a plain page that stubs that
API and sets the theme variables. It's extracted from the installed toolkit on start, never
copied into this repo, so it always matches the pinned judge version. It already works,
checked in headless Chrome on a real replay: board, playback, timeline and the viewer's own
per-team stats (deaths by kind, splits, sprints). The same bundle also has a map view
(`data-view="map"`) for the map previews.

## 8. The server bridge (competition server)

The arena is the team's one window on the competition server. It doesn't pass requests
through. It talks to the server itself, with one member's key on the documented API
(`/api/v1`, 120 requests a minute per key) and our `bcweb` client for what only the website
has. It then serves the results as its own endpoints under `/api/v1/server/*`, in the
arena's conventions: small flat JSON, our side already worked out, and server games linked
to arena submissions and replays. Teammates and their agents never see the key, and dozens
of them reading at once cost the server one cached request.

**1. What it exposes.** The union of what the official API gives and what `bcweb` adds:

| Endpoint | Returns | Comes from |
|---|---|---|
| `GET /api/v1/server` | Summary: live bot (server version and arena id), rank, rating, record, quota left, auto-submit state and candidate | both |
| `GET /api/v1/server/leaderboard?page=` | The ladder, with our row marked | API `/leaderboard` |
| `GET /api/v1/server/ratings?team=` | Rating history, ours or a team's | API `/ratings` |
| `GET /api/v1/server/teams/{team}` | A team's profile and recent battles. `{team}` is an id, a name (unique prefix) or `#rank` | API `/teams/:id` |
| `GET /api/v1/server/submissions` | Our versions on the server: W-D-L, per-map record, which arena submission each one is, which is live | API `/submissions` + `bcweb submissions --maps` |
| `GET /api/v1/server/battles?kind=&opponent=&result=&limit=` | Our battles: opponent and rating, kind (ranked, unranked, scrim, auto), our side, W-L, rating change, per-game map and result, queue position if unplayed | API `/battles` + `bcweb battles --maps` |
| `GET /api/v1/server/battles/{id}` | One battle: each game's map, seats, winner, end reason, totals, the crash or timeout `log`, and the arena game id (for stats and the viewer) | API `/battles/:id` |
| `POST /api/v1/server/battles` | Challenge: `{team, ranked, maps}`. Checks the quota first and refuses with the minutes until it frees up. Logged with who asked | API `POST /battles` |
| `GET /api/v1/server/quota` | Games left this hour, when more free up, and recent challenges with who started them | `bcweb quota` (site availability) |
| `GET /api/v1/server/maps` | The maps in play, with their text and arena per-map stats | API `/maps` |
| `GET /api/v1/server/tournaments`, `/tournaments/{id}` | Tournaments and brackets round by round, with each game's map and winner, and our matches marked | API `/tournaments` + `bcweb bracket` |
| `GET /api/v1/server/queue` | The judge's capacity and queue length | API `/queue` |
| `POST /api/v1/server/pause`, `/resume` | Pause or resume auto-submit. Logged with who | arena |

Replays of server and tournament games are arena games (below), so they're served by
`/api/v1/games/{id}/replay` and watchable in the browser viewer like any other.

Deliberately not exposed: activating a submission (only auto-submit changes the live bot),
uploading to the server (same), and anything touching team settings or keys.

**2. Caching and limits.** One key serves the whole team, so every read is cached:
- leaderboard and ratings for 60 s (the server allows 30 a minute for those);
- battles and quota for 15 s;
- finished battles, replays and past tournaments forever.

On a 429 the cached copy is served with `"stale": true`. The bridge's own background polling
(below) keeps the common reads warm, so most requests never reach the server.

**3. Auto-submit and auto-download.**
- **Auto-submit.** When an arena submission passes the champion gate, the bridge uploads it
  to the real server (`POST /submissions`, named after its arena id, its note as the
  description) and activates it. The gate:
  - #1 on the arena leaderboard, with its rating's lower bound above the live bot's upper
    bound;
  - at least 55% head-to-head against the live bot over at least 60 games;
  - no worse on the holdout maps and on the server suite;
  - no errors, CPU p99 under 70M points.

  It submits at most once every `min_hours` (default 4), well inside the site's 12 uploads
  an hour. Every upload, and every candidate held back with the failed condition, is
  logged. Any teammate can pause it from the website, and the log records who did.
- **Auto-download.** It polls `GET /battles` once a minute. For every finished game it
  downloads the replay (following the signed redirect without sending the key on), and
  keeps the battle's `log` (why a game crashed or timed out). Each game becomes an arena
  game with `suite = 'server'`:
  - our seat holds the arena submission that was live;
  - the other seat holds the external bot `server:<team name>`;
  - every server game opens in the browser viewer and gets the same stats as local games.
- **The server suite.** Every map and seed seen in a server game joins a `server` map suite.
  Map text comes from `GET /maps`, or from the replay for maps no longer in play. The champion
  gate plays candidates there, so a bot that only wins on our maps doesn't go live (the v9
  lesson).

**4. The `/server` page** shows people the same, on one page:
- the live bot and its arena id;
- rank and rating over time;
- battles with per-map results and "watch" links;
- quota left;
- the current candidate and why it is or isn't going up;
- the upload history.

**Secrets.** The bridge needs, on poscatgpu2, mode 600 and outside the data directory:
- one member's key from the site's Team page (today `~/.unswbc/keys.json` on the Mac);
- the website login `bcweb` uses (`~/.unswbc-web/credentials`), for quota, per-map detail
  and brackets.

Only the bridge reads them, and it never logs or serves them. Everything done with them
is recorded on the site under that member's name. Copying them there is the user's step.

## 9. Deployment on poscatgpu2

- **Container:** one image (`Dockerfile`: Python 3.12, uv, the pinned unswbc, the arena, and
  `bcweb` from a build context). `compose.yaml` runs it next to a **Tailscale sidecar**
  (`tailscale/tailscale`, userspace networking, so no sudo and nothing installed on the
  host).
  - The arena shares the sidecar's network namespace and listens on `127.0.0.1:8700` only.
    The one way in is `tailscale serve` (`deploy/serve.json`), which is HTTPS on the tailnet
    at `https://tokenz-arena.<tailnet>.ts.net` and adds `Tailscale-User-Login`.
  - The container runs as the host user, so files in the data directory stay theirs.
- **State** lives in `ARENA_DATA` (`~/tokenz-arena-data`): `arena.toml`, `submissions/`,
  `games/`, `replays/`, `bots/`, `maps/{rotating,holdout,server}/`, `server/`, `ratings.json`.
  The judge's toolchain and wasm build cache live in `ARENA_CACHE`. Secrets (the server key)
  are in `ARENA_SECRETS`, mounted read-only. Back up `ARENA_DATA` with `rsync`.
- **CI** (`.github/workflows/ci.yml`): `uv sync --locked --extra server` and pytest on every
  push and PR. It reads `bcweb` with a read-only deploy key (secret `BCWEB_DEPLOY_KEY`).
- **CD** is pull-based, so poscatgpu2 needs no inbound access and GitHub holds no server
  secrets. `arena-update.timer` (systemd user timer, every 3 minutes) runs
  `deploy/update.sh`:
  1. fast-forward `tokenz-arena` and `bcweb` to `origin/main`, using read-only deploy keys
     (`~/.ssh/deploy-*`, host aliases `github-tokenz-arena` and `github-bcweb`);
  2. if either moved: rebuild the image, smoke-test it, then `docker compose up -d`.

  A failed build or smoke test leaves the running arena alone. A restart re-queues the
  games that were running.
- **Disk:** the root volume had 106 GB free on 2026-10-01. Replay retention (section 5)
  keeps it bounded.

## Build order

1. Runner and stats: one game end to end, parsed (`arena play <map> <dirA> <dirB>`).
   Measure a real 500-round game's cost on poscatgpu2.
2. The JSON store, `POST /submissions` with checks, `GET /submissions/{id}`.
3. Scheduler and worker pool: drafts and placement; `?wait=`.
4. Ratings, reports, leaderboard, `arena status`.
5. Replay and source downloads, games, `/ideas`, `/llms.txt`.
5b. The website: leaderboard, submission, game (with the viewer) and submit pages first.
6. MCP.
7. Deploy on poscatgpu2 (container + Tailscale sidecar, pull-based CD); seed it with our best bots.
8. Holdout, continuous matches, retention, the remaining web pages.
9. The server bridge, in this order: the read endpoints and auto-download (read-only);
   the server suite; challenges; auto-submit. The key and the web login go on poscatgpu2 by
   hand.
10. `docs/AGENTS.md`: the brief for teammates' agents (the loop above, with curl examples).

## Open decisions

- The draft size (parent + top 3 × official maps × 2 seats = 120 games) depends on the
  measured cost of a game.
- Should `promote` require beating the parent, or is it the owner's call?
- Should the bridge also spend quota on its own (ranked battles to climb), or leave all
  challenges to teammates (`POST /api/v1/server/battles`)?
- Should our syeyoung-tokens variants enter through `make-bot.sh` exports, as one bot each?
