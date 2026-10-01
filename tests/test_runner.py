from arena.runner import parse

# Real output of `unswbc run --sandbox --seed 1` (1.2.3), two `unswbc init python` bots on Colosseum.
LOG = """loaded python vs python in the judge's sandbox
seed 0x0000000000000001
running round 51/500 --  last: 0ms  eta: 16.2s  dragons: 1 vs 1
round 67: bot 1 (team B) died: hit a wall
team A wins after 68 rounds (by elimination) (1.8s)
team A points per turn: p50 5.2M  p99 20.4M  mean 5.4M  max 20.4M  (68 turns)
team B points per turn: p50 5.1M  p99 20.4M  mean 5.4M  max 20.4M  (68 turns)
wrote replay: out/a-vs-b-on-Colosseum-2026-10-01-202847.replay
"""


def test_parse_win():
    r = parse(LOG)
    assert (r.winner, r.rounds, r.reason) == ("A", 68, "elimination")
    assert [(d.round, d.bot, d.team, d.cause) for d in r.deaths] == [(67, 1, "B", "hit a wall")]
    assert r.points["B"]["p50"] == 5.1e6 and r.points["A"]["max"] == 20.4e6
    assert r.replay.name == "a-vs-b-on-Colosseum-2026-10-01-202847.replay"
