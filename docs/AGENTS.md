# Using tokenz-arena from an agent

Give this to your worker agents. It explains how to measure a bot change on the team's arena
and what rules to follow. The full API reference is at `<arena>/llms.txt`.

`<arena>` is the arena's tailnet address, e.g. `https://poscatgpu2.<tailnet>.ts.net`.

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

- **MCP** (preferred): streamable HTTP at `<arena>/mcp`.
  `claude mcp add --transport http arena <arena>/mcp`
  The tools are `submit`, `submission`, `wait`, `promote`, `report`, `source`,
  `leaderboard`, `bot`, `ideas`, `games`, `game`, `map_stats`, `queue`, and `server_*` for
  the competition server.
- **curl**: JSON under `<arena>/api/v1`; see `<arena>/llms.txt`.

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
   something.
5. **Submit a draft** that names the parent and has a note giving the hypothesis and the
   change:
   - MCP: `submit(bot="swarm", parent="swarm-6", note="Hypothesis: ... Change: ...",
     files={...}, agent="worker-3")`
   - curl:
     ```sh
     (cd mybot && zip -r ../bot.zip .)
     curl -F file=@bot.zip -F bot=swarm -F parent=swarm-6 -F agent=worker-3 \
          -F note="Hypothesis: lunging at adjacent enemy heads wins trades. Change: ..." \
          <arena>/api/v1/submissions
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
- **Don't flood the queue.** Each owner can have a limited number of drafts waiting (8 by
  default). Past that, submitting returns HTTP 429, so wait for some to finish. All your
  agents share this cap because they share your login.
- **Always name a parent** when you build on a bot, and write a real note. The note should
  say what you changed and why you expect it to win.
- Submissions are immutable. Re-submitting identical bytes returns the existing submission
  (`"duplicate": true`).
- Don't `promote` something that didn't beat its parent unless your brief says to.
