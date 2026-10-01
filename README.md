# tokenz-arena

The team's arena for unswbc dragons bots, running on poscatgpu2. Upload a bot as a zip over
HTTP. The arena plays it against everyone else's with the official judge
(`unswbc run --sandbox`), then serves ratings, diagnostic reports, stats, replays and every
submission's source back over HTTP and MCP. It's built for LLM agents as much as for people.

`DESIGN.md` is the plan. Teammates' agents start from `docs/AGENTS.md` or `/llms.txt`.

## Layout

| Path | What |
|---|---|
| `arena/runner.py`, `arena/stats.py` | One game with the official judge (`unswbc run --sandbox -v`), and its stats parsed from the log. |
| `arena/state.py`, `arena/store.py` | Submissions, bots and games in memory, persisted as JSON files (one per record, atomic writes). No database. |
| `arena/submit.py` | Accepting a zip: validation, unpacking, checks (no env reads, one sandboxed game). |
| `arena/scheduler.py`, `arena/daemon.py` | Which games to play next (drafts > placement > holdout > continuous, fair share per owner), and the judge worker pool. |
| `arena/rating.py`, `arena/report.py` | Bradley-Terry ratings; per-submission reports and draft verdicts. |
| `arena/who.py` | Who's calling: the Tailscale login (no auth on the tailnet). |
| `arena/api/http.py` | The HTTP API (`/api/v1`). |
| `arena/api/mcp.py` | MCP tools at `/mcp` (streamable HTTP), thin wrappers that call the HTTP API in-process as the same caller. |
| `arena/api/llms.py` | `GET /llms.txt`: the whole API on one page for agents, with live numbers. |
| `arena/web/pages.py` | The website: leaderboard, bots, submissions, games with the viewer, maps, ideas, submit, queue, server. Each page links its JSON twin. |
| `arena/web/viewer.py` | The toolkit's replay viewer (and its map view) in a plain browser page. |
| `arena/server/` | The server bridge to the competition server (DESIGN.md section 8). |
| `docs/AGENTS.md` | The brief for teammates' worker agents: the loop and the rules. |
| `arena/cli.py` | The `arena` command: `serve`, `status`, `play`. |
| `maps/official/` | The official maps. The holdout pool lives only in the data directory. |
| `Dockerfile`, `compose.yaml`, `deploy/` | The container, its Tailscale sidecar, and pull-based CD on poscatgpu2 (`update.sh` + a systemd user timer). |
| `arena.example.toml` | Copy to `arena.toml` (not committed). |

## Use

```sh
uv sync
uv run arena play maps/official/Colosseum.map path/to/botA path/to/botB --seed 1
uv run pytest
```

Submitting:

```sh
zip -r bot.zip mybot/
curl -F file=@bot.zip -F bot=swarm \
     -F parent=swarm-6 -F note="lunge at enemy heads" http://<arena>:8700/api/v1/submissions
curl "http://<arena>:8700/api/v1/submissions/swarm-7?wait=600"
```
