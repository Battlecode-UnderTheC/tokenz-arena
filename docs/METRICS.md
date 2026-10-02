# In-game metrics

The arena replays every game's replay and records **metrics** for both teams every 10 rounds
(and at the last round): how many dragons each side has, who controls the board, the queen,
pearls eaten, deaths, and so on. Derived metrics combine them; the built-in one is the **eval
bar**, the chance team A wins at that round.

Where you see them:

| Where | What |
|---|---|
| A game's page (`/games/<id>`) | Under the header: the eval bar at the viewer's current round (it follows playback and scrubbing), the eval over the whole game, and **metrics ▾**: a small chart of every metric for both teams, with the values at the current round. |
| A submission's page (`/s/<id>`) | **In-game metrics**: each metric for this bot vs its opponents at rounds 50-400 and at the end, averaged over its games. Green where it's ahead. |
| API | `GET /api/v1/metrics` (what exists), `GET /api/v1/games/<id>/metrics` (the series), `GET /api/v1/submissions/<id>/metrics` (the averages). MCP: `metrics`, `game_metrics`, `submission_metrics`. |

They're computed right after each game on whichever machine played it (about 1-4 seconds, a
few percent of a game), and for older games by a low-priority background task.

## What predicts winning (and why these metrics)

From ~8,500 arena games and 258 real competition games under unswbc 1.2.3:

- **Rounds 50-350: board control and dragons alive.** The share of tiles your heads reach first
  (`board_control`) and log(1 + dragons alive) (`log_dragons`) predict the winner best (AUC
  0.80-0.84 on real games). Total length and pearls eaten follow.
- **Rounds 400-490: the longest dragon** decides round 500 in ~95% of games that get there.
- **Queens rarely decide**: both are dead in ~93% of games that reach round 500. But when only one
  survives to the end, that side wins ~98% of the time.
- **Kills on their own predict nothing**, and neither does not dying: deaths go slightly *with*
  winning (bots that split and expand lose more dragons), so they aren't coloured.

**Among the top teams it's different: queens matter.** 667 games from 127 series between other
teams (mean Elo ~1770), crawled from the competition server on 2026-10-02:

| | top teams | arena / our server games |
|---|---|---|
| ended by elimination | 42% | rare |
| round 500 decided by the queen | 25% (22% with only one queen alive) | ~4% |
| both queens dead at round 500 | 51% of games reaching it | 92-94% |
| P(win), only my queen alive at r200 / r300 / r400 / r490 | 75% / 84% / 92% / 99% | |

Queen alive / queen length is the best predictor from round ~300 on (AUC 0.73 at r300, 0.83 at
r490), board control and dragons alive before that (0.75 at r100). The eval (v2) is fitted on top-team, server and arena games weighted equally,
so it gives the queen about twice v1's weight from round 200 on. Out of fold it calls the winner of
top-team games 70% of the time at r100, 75% at r300, 81% at r400 and 96% at r490 (v1: 70, 68,
74, 89%); of our server games 72, 76, 77 and 95%.

The queen metrics `queen_room` (free tiles near the queen's head), `queen_danger` (steps from the
nearest enemy head) and `queen_share` (its share of the team's length) describe how a bot treats its
queen; they didn't predict the winner any better than whether the queen is alive.

The eval combines dragons (log), board control, longest dragon, queen alive and who leads under
the round-500 rule, with weights fitted per round (`arena/metrics/plugins/eval.py`).

## Comparing two bots

`/compare?a=X&b=Y` (linked from each submission's parent, and in the top menu) puts two bots side
by side from their games' metrics, each from its own seat: per map (score, length, how often the
queen dies), every 10 rounds (each metric, theirs and their opponents', averaged over the games
still running), their queens (survival by round, how they died: wall, own body, enemy or ally
body, head-on and who moved, lunged by an enemy, no valid move; and when: early 1-150, mid
151-350, late 351-500), and early/mid/late averages. Filter by map, or keep only the two bots'
games against each other. JSON: `GET /api/v1/compare`.

## Adding a metric

Put a function in any module under `arena/metrics/plugins/` (new files are picked up
automatically):

```python
from arena.metrics import metric

@metric("tail_room", "Room behind the queen", "free tiles within 6 steps of the queen's head",
        version=1, unit="tiles", better="higher")
def tail_room(ctx, team):
    q = ctx.queen(team)
    if q is None:
        return 0
    head = ctx.st.body[q][0]
    return len(ctx.bfs([head], blocked=ctx.occupied() - {head}, limit=6))
```

It's called for each team at every snapshot round with a `ctx`:

| `ctx.` | |
|---|---|
| `st` | the replayed state: `st.body[id]` (tiles, head first), `st.team[id]`, `st.alive`, `st.pearls`, `st.round` |
| `g` | the map: `g.W`, `g.H`, `g.step(tile, dir)` (follows kelp and portals) |
| `rnd` | rounds played so far |
| `ids(t)`, `lengths(t)`, `heads(t)`, `queen(t)`, `other(t)` | per-team helpers |
| `cum[t]` | counters since the start: `eat`, `deaths`, `deaths_enemy`, `deaths_self`, `lost`, `splits`, `kills`, `paid` |
| `rate(t, key, window)` | how fast a counter (`eat`, `deaths`, `kills`, `splits`) grew per round over the last `window` rounds, e.g. `pearl_rate` = `ctx.rate(t, "eat", 50)` |
| `occupied()`, `bfs(sources, blocked, limit)`, `head_dist(t)` | spatial helpers (cached per round where it makes sense) |
| `cache` | a dict shared by all metrics at this round, for anything expensive |

Return a number (or `None`). `better` (`higher`, `lower` or `none`) only colours the summaries.

A **derived** metric combines the others' values at a round into one number for the game:

```python
from arena.metrics import derived

@derived("length_share", "Length share", "A's share of all length on the board")
def length_share(values, rnd):          # values: {metric: {"A": x, "B": y}}
    a, b = values["total_length"]["A"], values["total_length"]["B"]
    return a / (a + b) if a + b else 0.5
```

A derived metric fitted on games (like the eval) should say `fitted="<ISO time of the newest game
the fit saw>"`: the evals (/evals) then judge it only on games after that, out of sample.

**Changing** a metric: bump its `version`. The background task recomputes it for older games
(newest first, real competition games before local ones). A metric that raises is recorded in
the file's `errors` and doesn't stop the others.

Check one locally before pushing:

```sh
uv run python -c "from arena import metrics; import json; \
  print(json.dumps(metrics.compute(open('tests/data/colosseum-seed1.replay','rb').read())['team']['tail_room']))"
```
