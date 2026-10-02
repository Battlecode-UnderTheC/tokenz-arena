# Ladder: every bot against the field (design, not built)

Status: proposed, 2026-10-02. Nothing here is implemented yet.

## Why

The official leaderboard rates only each bot's current ranked submission, and a draft plays only
its parent and the top 3, nearly all from the same family. That hides four things the ladder
should show:

1. **Varied opponents for drafts.** A change can win by exploiting our own family's habits; the
   rest of the pool (swarms, body-wall, late-surge, search lines, teammates' bots) is the
   closest thing we have to other teams without the server.
2. **Forgotten strong bots.** Old imports never rated under the current rules may be good at
   something we've lost.
3. **Non-transitivity.** A beats B beats C beats A only shows when everyone meets everyone's
   neighbours, and it's the "wins locally, loses on the server" trap.
4. **An absolute number for every attempt,** so ideas and lineage show which lines climb.

It must not slow anyone's verdicts, and it doesn't replace the official leaderboard.

## What stays exactly as it is

- The official ratings (`ratings.json`, `rating.fit`: current ranked submissions only), the
  leaderboard's ranked table, promotion, and the auto-submit gate.
- Drafts' and placement's owed games (parent + top 3, both seats, all draft maps).
- The queue's order: drafts, placement, holdout, then ranked bots' remaining placement games.

## The pool

Every submission that passed its check and whose bot folder exists: drafts (queued, playing,
done), placing, rated, and retired or replaced versions. Not rejected ones, not the stand-ins
for server opponents (`server:<team>`). Today: 243 bots.

**Family:** the bot name of the submission's lineage root (its oldest ancestor in the arena),
so gst-queen-3 and its drafts are one family, swarm-hunt another.

## The ladder rating

- **One Bradley-Terry fit over every counting game between pool members, from every lane**:
  draft, placement, continuous and ladder games all are evidence. "Counting" is what it is
  today: current rules, current map content, no errors, no self-play, not holdout.
- Same model as the official fit (a draw is half a win, one virtual draw per pair that has met,
  95% ± from the Fisher information), on its own scale: mean 1500. A bot with no counting games
  has no rating yet ("unrated").
- Refit by the engine every 5 minutes (`[ladder] refit_minutes`) and written to
  `data/ladder.json`: `{sid: {elo, err, games, opponents, last_game}}`, plus the contradictions
  below. Separate from `ratings.json`; nothing official reads it.
- Cost: ~240 bots, ~30k games, a few thousand distinct pairs; the fit runs on pair counts, well
  under a second.

## Ladder games

**Priority: last.** They take only slots nothing else wants: after drafts, placement, holdout
and ranked bots' remaining placement games. They **replace** today's endless background pairing
of ranked bots (`scheduler._continuous`); ranked bots are in the pool, so they keep getting
games. Ladder games aren't owed, so they never count as backlog and never make the scaler rent.
Remote workers play them (they're on public maps).

**Lane:** a new `ladder` lane (priority 4), added to the rated lanes, so ladder games between two
current ranked bots also count in the official ratings, exactly as background games do today.

**Pairing, Swiss-style and sparse.** When the queue reaches the ladder, it builds pairings one
at a time until the free slots are filled:

1. **Who plays next:** the pool bot with the highest need =
   `uncertainty × staleness × tier`:
   - uncertainty: its `err` (unrated bots get the maximum, so they're placed first);
   - staleness: grows with time since its last ladder game, so nobody is starved;
   - tier: 0.2 for bots clearly at the bottom (rating + err below the median − 200), else 1.
2. **Against whom:** the opponent with the best
   `closeness ÷ (1 + games already played between the two)`, where closeness falls off with
   the rating gap (a 200-point gap halves it), and a bot from **another family** gets twice the
   weight. Ties are broken by a seeded random choice, so pairings are reproducible in tests.
3. **The block:** 2 of the competition server's maps (with their real seeds), chosen at random
   per pair, both seats: **4 games**. A pair that has already played those keys gets 2 other
   maps.

At the arena's current speed the ladder would add roughly 30–60 games per bot over a day of idle
time, not the ~29,000 pairings of a full round-robin.

## Drafts play "the field"

When a draft starts (when its parent + top 3 are frozen), it also freezes **4 field opponents**
(`Submission.field`, a new list, None until frozen):

- from the ladder's upper half (by ladder rating, rated bots only);
- each from a **different family**, none from the draft's own family, none already among its
  parent + top 3;
- chosen at random, seeded by the draft's id.

Each field opponent plays **4 of the server's maps (with their seeds), both seats = 8 games**, at
draft priority: **32 games per draft** on top of today's ~312. They're part of what the draft
owes, so the draft isn't "done" until they're played, and they count as backlog like the rest.

The draft verdict (`report.verdict`, the "Draft verdict" panel and the API) gains a **"vs the
field"** row: the score over those 32 games with its 95% interval, and per field opponent.

## What you see

- **Leaderboard:** a **Ladder** tab next to the official table: every pool bot with ladder
  rating ± err, games, family, status (draft, rated, retired…), its official rank if it has one,
  and when it last played. Sorted by rating; unrated bots at the bottom.
- **Contradictions** on that tab: pairs with at least 8 head-to-head games whose result
  contradicts the rating order with confidence (the head-to-head 95% interval excludes 50% on
  the wrong side). That's where cycles like A > B > C > A show up.
- **Submission page:** its ladder rating; for drafts, the "vs the field" verdict.
- **Ideas and Lineage:** each attempt's ladder rating beside it.
- **API:** `GET /api/v1/ladder` (ratings, contradictions); MCP `ladder()`; `/llms.txt` updated.
- **Queue:** ladder games show with lane `ladder`.

## Settings (`[ladder]`, all optional)

| key | default | |
|---|---|---|
| `enabled` | true | false: no ladder games (the old background pairing comes back) and no field opponents. |
| `refit_minutes` | 5 | How often the ladder rating is refit. |
| `block_maps` | 2 | Server maps per ladder pairing (× 2 seats). |
| `bottom_weight` | 0.2 | How often clearly bottom bots are picked, relative to others. |
| `field_size` | 4 | Field opponents per draft. |
| `field_maps` | 4 | Server maps per field opponent (× 2 seats). |

## Changes, by file

| file | change |
|---|---|
| `arena/ladder.py` (new) | pool, families, the fit, contradictions, `ladder.json`, the pairing picker |
| `arena/rating.py` | the Bradley-Terry core factored out so both fits share it (official results unchanged) |
| `arena/scheduler.py` | `LADDER` priority and lane; drafts freeze `field` and owe its games |
| `arena/queue.py` | when nothing is owed: holdout, then ladder pairings instead of `_continuous` |
| `arena/state.py` | `Submission.field`; `ladder` in the rated lanes |
| `arena/report.py` | "vs the field" in the draft verdict |
| `arena/daemon.py` | refit the ladder every `refit_minutes` |
| `arena/api/http.py`, `mcp.py`, `llms.py` | `/api/v1/ladder`, MCP tool, docs |
| `arena/web/pages.py` | Ladder tab, contradictions, ladder ratings on submission, ideas and lineage pages |

## Tests

- The factored fit gives the official ratings exactly as today on existing fixtures.
- The ladder fit includes drafts and retired bots; unrated bots stay unrated.
- Pairing: unrated and uncertain bots first, cross-family preferred, nothing already played or
  running, reproducible with a seed, bottom bots picked less.
- Ladder games appear only when nothing is owed, never count as backlog (the scaler doesn't
  react), and are leased to remote workers.
- A draft freezes 4 field opponents from 4 other families once, owes 32 extra games, and its
  verdict shows "vs the field".
- `enabled = false` restores the old background pairing.
