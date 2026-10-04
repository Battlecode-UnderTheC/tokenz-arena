# Duels and ephemeral bots

Ad-hoc A-vs-B testing on fresh seeds, played by the arena instead of your laptop, that never
touches the ladder. Use it to try a change quickly before submitting a draft.

## In short

```sh
tokenz ephemeral ./my-bot --name try-split            # -> eph-3f9a1c2b (compiled, ready)
tokenz duel queen-guard-7 eph-3f9a1c2b --seeds random:8 --wait
tokenz duel show duel-91be02d4 · tokenz duel cancel duel-91be02d4 · tokenz duel ls
```

MCP: `ephemeral(name, files | zip_base64)`, `duel(a, b, ...)`, `duel_result(id, wait_s)`,
`duel_cancel(id)`. HTTP: `/api/v1/ephemeral` and `/api/v1/duels` (`/llms.txt`).

## Ephemeral bots

A zip checked like a submission (safe zip, bot.toml, no environment reads, compiled with the
judge's clang) but not a submission: it has no rating and doesn't appear on the leaderboard,
in ideas, lineage or `/submissions`. It is usable only in duels and is removed after its TTL
(24 h by default, `ttl_hours`), unless a duel that uses it is still running. Uploading the same
zip again returns the same bot and renews it. There's no check game: the duel's games are the
test.

## Duels

`a` and `b` are any two bots: submissions (drafts too) or ephemeral bots.

- **Maps:** the suites asked for (official, tournament, server, custom, rotating; default the
  first three), optionally only some map names. Holdout maps are never used. The maps are
  copied into the duel when it's created, so a new rotating week can't take them away.
- **Seeds:** `random:N` (default 4) draws N fresh random seeds per map, so a bot can't be
  tuned to the ladder's fixed seeds. A list (`"1,2,3"`) replays chosen seeds.
- **Seats:** each game is also played with the seats swapped unless `both_seats` is false.
  `games` cuts the schedule short; the order goes one seed round at a time over a shuffled
  map list, so a cut keeps the maps balanced.
- **Priority:** after drafts and placement, before the rated top-up and the idle filler. Duel
  games count as backlog, so a busy arena rents machines for them (within the scaler's $1/h and
  $10/day caps and 6 machines). There are no per-user limits.
- **Judging:** like every game (cppjudge where it can, the official judge otherwise, with the
  1-in-50 spot check), on the engine's own slots or leased to workers.

### The result

`GET /api/v1/duels/{id}?wait=S` (`duel_result`) answers from a's side:

- `status` (waiting for a compile, queued, playing, done, cancelled, failed), played, left,
  running and error counts;
- `record`: W-D-L, score and 95% interval, and whether it excludes 50% (`significant`; if not,
  the two bots are within noise);
- `by_suite`, `by_map`, `by_end` (how games were decided: elimination, longer queen, longest
  dragon, total length, draw) and `by_starters` (maps with ≤2 or 3+ starting dragons a side);
- `sides.{a,b}`: each bot's metrics from its own seat, averaged at rounds 100-400 and at the
  end (dragons, lengths, board control, pearl rate, queen, tiles visited and seen), its queen's
  survival and death causes, deaths by cause, and CPU (max, p99, overruns);
- `games`: each game's map, seed, seats, result, and replay and watch links
  (`/duels/{id}/watch/{n}`).

## Isolation

Duel games are never arena `Game`s: they're stored in `data_dir/duels/<id>/` (games, metrics,
replays, pinned maps), so ratings, verdicts, beats-parent, the auto-submit gate, ideas, lineage,
the evals and the metrics backfill can't see them. Replays are kept 3 days after a duel ends
(`[duels] replay_days`), results 30 days (`keep_days`).
