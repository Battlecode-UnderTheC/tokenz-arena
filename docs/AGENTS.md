# Using tokenz-arena from an agent

Give this to your worker agents. It explains how to measure a bot change on the team's arena
and what rules to follow. The full API reference is at `{ARENA}/llms.txt`.

`{ARENA}` is the arena's tailnet address: {ARENA}

## What the arena is

A shared league for UNSW Battlecode "dragons" bots. You upload a bot as a zip. The arena
plays it with the official judge (`unswbc run --sandbox`, the competition's version)
against its parent and the current top bots. It then gives you a verdict, a diagnostic
report, replays, and every submission's source. Every teammate's agents use the same
arena, so the submission history is the team's experiment log.

## Connecting

There's no key or login. The arena is reachable only on the tailnet, and it knows you by
your Tailscale login. Anything running on your machine submits as you. To tell your own
agents apart, label each one with `agent` (e.g. `worker-3`). The label is shown next to your
name and you can filter by it.

- **MCP** (preferred): streamable HTTP at `{ARENA}/mcp`.
  `claude mcp add --transport http tokenz-arena {ARENA}/mcp`
  The tools are `submit`, `submission`, `wait`, `promote`, `report`, `source`,
  `leaderboard`, `bot`, `ideas`, `games`, `game`, `map_stats`, `queue`, and `server_*` for
  the competition server.
- **`tokenz`** (if you have a shell): `curl -fsSL {ARENA}/tokenz.py -o tokenz && chmod +x tokenz`,
  then `./tokenz --json --agent worker-3 <command>`. For example: `submit DIR --bot B --parent P
  --note "..." --wait`, `report ID`, `get ID DIR`, `ideas WORDS`, `promote ID`.
- **curl**: JSON under `{ARENA}/api/v1`; see `{ARENA}/llms.txt`.

## The workflow

1. **Check what's been tried.** `ideas(q="<your topic>")` or
   `GET /api/v1/ideas?q=<topic>`. If the same idea already lost to its parent, change your
   approach or pick another idea.
2. **Start from a strong bot.** `leaderboard()` and pick the top submission (or the one
   your brief names). `source(id)` returns its files as `{path: text}`. With curl, use
   `GET /api/v1/submissions/<id>/source`, which returns the zip. Don't write a bot from
   scratch: rewrites lose behaviours the strong line has built up.
3. **Read where it loses.** `report(id)` shows the worst maps and opponents, how its
   dragons die, CPU headroom, and the worst losses with replays.
4. **Change one thing.** One change per submission is what makes the verdict mean
   something. To try variations quickly first, upload them as ephemeral bots and duel them
   against the parent on fresh seeds: `ephemeral(name, files)` then `duel(parent, "eph-...",
   seeds="random:8")` and `duel_result(id, wait_s=1800)`. Duels never touch the ladder or
   `/ideas`, and the arena plays them instead of your machine (docs/DUELS.md). Submit only the
   variation that wins.
5. **Submit a draft** that names the parent and has a note giving the hypothesis and the
   change:
   - MCP: `submit(bot="swarm", parent="swarm-6", note="Hypothesis: ... Change: ...",
     files={...}, agent="worker-3")`
   - curl:
     ```sh
     (cd mybot && zip -r ../bot.zip .)
     curl -F file=@bot.zip -F bot=swarm -F parent=swarm-6 -F agent=worker-3 \
          -F note="Hypothesis: lunging at adjacent enemy heads wins trades. Change: ..." \
          {ARENA}/api/v1/submissions
     ```
   A draft plays its parent and the top 3 on the public maps, in both seats. It's unranked
   and usually takes a few minutes.
6. **Wait for the verdict.** `wait(id)` or `GET /api/v1/submissions/<id>?wait=600`. If
   `"finished"` is false (MCP) or the status isn't final yet, wait again.
   - `status == "rejected"`: read `reason` and `check_log`, fix the problem, and resubmit.
   - Otherwise read `verdict.vs_parent`: `{W, D, L, score, beats_parent}`.
7. **Promote only if it beats its parent** (`beats_parent == true`): `promote(id)` or
   `POST /api/v1/submissions/<id>/promote`. It then gets full placement and joins the
   leaderboard. If it loses or ties, don't promote. Its note and numbers stay in `/ideas`,
   so the next agent doesn't repeat the experiment.
8. **Report back** to whoever briefed you: the submission id, the verdict against the
   parent, and whether you promoted it.

The outcomes listed in `/ideas` are `beat_parent`, `lost_to_parent`, `tied_parent`,
`no_parent`, `pending` and `rejected`. Filter with `?outcome=`, and `promoted` also works.

## Rules

- **No environment-variable reads in bots.** This means no `os.environ`, `os.getenv` or
  `getenv`. The arena rejects them, and so does the competition server.
- **`bot.toml` at the zip's root** (or one folder down), with `[project] language =` set to
  `"py"`, `"python"`, `"c"` or `"cpp"`.
- **Zip ≤ 4 MB.** Leave out build output, replays and logs.
- **CPU:** stay well under the 100M points-per-turn cap. `report(id).cpu` shows the
  headroom.
- **The queue is shared fairly.** There's no limit on drafts, but owners take turns on the
  workers, so if your agents queue many, theirs wait behind each other, not behind everyone.
- **Always name a parent** when you build on a bot, and write a real note. The note should
  say what you changed and why you expect it to win.
- Submissions are immutable. Re-submitting identical bytes returns the existing submission
  (`"duplicate": true`).
- Don't `promote` something that didn't beat its parent unless your brief says to.
