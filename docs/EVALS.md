# Evals: our metrics against real games

The arena's metrics (see Metrics) are fitted on games we have. **Evals** keeps checking them against
games we didn't fit on: finished battles between *other* teams on the competition server,
downloaded as they happen. Page: **/evals**. API: `GET /api/v1/evals?window=5h|12h|1d|3d|all`. MCP:
`evals(window)`.

## What the page shows

| Section | |
|---|---|
| Does the eval pick the winner? | Per round: accuracy (eval > 50% picks the winner), AUC, log loss, Brier, and a baseline: whoever leads under the round-500 rule right now. If the eval isn't above the baseline, it isn't telling you anything. |
| Calibration | When the eval says 70%, does seat A win about 70% of the time? Early, mid and late game. Dots below the diagonal mean it's overconfident. |
| Each metric | AUC of (A − B) for "A wins" at r50 to r490 and at the end. 0.5 is a coin flip. Green means the metric points the way you'd expect. |
| How games end, Queens | Elimination vs round 500, how often the queen decides, and how often a lone surviving queen wins. |
| By strength | Eval accuracy by the weaker team's Elo. |
| Latest games | Each one opens in the viewer with the eval bar and all metrics. |

Windows: 5h, 12h, 1d (the default), 3d and all. The game moves fast, so the page opens with
**Across windows**: the eval's accuracy and AUC in every window side by side. When the top teams
change how they play, the short windows drop first.

## How it runs

`arena evals` is its own process (compose service `evals`). It isn't part of the engine or the website.
Every 5 minutes it reads the leaderboard and the last six battles of each of the top 40 teams
(not ours). It downloads every finished battle it hasn't seen and computes the metrics in two
low-priority processes. It sends at most one request every 3 s with the team's key (the engine
uses the rest of the key's 120 a minute) and waits out 429s. Files live under `data/evals/`
(see `arena/evals/__init__.py`). Replays are pruned oldest first past `keep_gb`; games and
metrics are kept.

When a metric changes version, the service recomputes it from the kept replays after it
restarts (every deploy restarts it).

```toml
[evals]                 # every key is optional
top_teams = 40          # how many of the leaderboard's teams to follow
min_elo = 0             # skip battles with a team below this
poll_seconds = 300
pace_seconds = 3        # between requests to the server
procs = 2               # metric processes (nice 15)
keep_gb = 30            # replays kept for recomputing
our_team_id = 328       # default: asked from the server
```

Import a crawl made earlier (`<dir>/M<battle>/battle.json` with `{listing, battle}`, plus
`M<game>.replay`):

```sh
docker compose run --rm -v ~/evalbar-work/top:/import:ro evals \
  arena evals --config /data/arena.toml --import /import
```
