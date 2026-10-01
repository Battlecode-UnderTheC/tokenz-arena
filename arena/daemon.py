"""The background half of `arena serve`: checks new submissions, schedules, plays, rates.

Workers call runner.play() in a pool sized by [play].workers and write each finished game as
games/<id>.json (store.py). On start, games that were running are simply queued again.
"""

# TODO(build order 3): main(config)
