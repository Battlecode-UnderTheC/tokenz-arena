# tokenz-arena

The team's arena for unswbc dragons bots, running on poscatgpu2. Upload a bot as a zip over
HTTP. The arena plays it against everyone else's with the official judge
(`unswbc run --sandbox`), then serves ratings, diagnostic reports, stats, replays and every
submission's source back over HTTP and MCP. It's built for LLM agents as much as for people.

`DESIGN.md` is the plan. Status: skeleton. `arena play` works; the rest follows the build
order in `DESIGN.md`.

## Layout

| Path | What |
|---|---|
| `arena/runner.py` | One game with the official judge, parsed (working). |
| `arena/store.py` | JSON files, one per record, atomic writes. No database. |
| `arena/submit.py`, `scheduler.py`, `stats.py`, `rating.py`, `report.py`, `daemon.py` | Submission checks, scheduling, stats, ratings, reports, the worker loop (stubs). |
| `arena/api/http.py`, `arena/api/mcp.py` | The HTTP API and MCP tools (stubs). |
| `arena/web/viewer.py` | The toolkit's replay viewer in a plain browser page (working). |
| `arena/cli.py` | The `arena` command. |
| `maps/official/` | The official maps. The holdout pool lives only in the data directory. |
| `deploy/arena.service` | The systemd user service for poscatgpu2. |
| `arena.example.toml` | Copy to `arena.toml` (not committed). |

## Use

```sh
uv sync
uv run arena play maps/official/Colosseum.map path/to/botA path/to/botB --seed 1
uv run pytest
```

Submitting (once the API is built):

```sh
zip -r bot.zip mybot/
curl -F file=@bot.zip -F bot=swarm \
     -F parent=swarm-6 -F note="lunge at enemy heads" http://<arena>:8700/api/v1/submissions
curl "http://<arena>:8700/api/v1/submissions/swarm-7?wait=600"
```
