"""The eval bar: P(team A wins) at each round, from a few metrics (see the analysis in
docs/METRICS.md). v2: weights fitted per round on 1,141 games between the competition's top teams,
435 of our server games and 3,980 arena games (each set weighted equally) under unswbc 1.2.3.
Out of fold it calls the winner in top-team games 70% of the time at r100, 75% at r300, 81% at
r400 and 96% at r490 (v1, fitted on arena games only: 70/68/74/89%). Queens weigh about twice as
much from round 200 on. The extra queen metrics (room, distance to the enemy) added nothing on top
of whether the queen is alive, so they're shown but not used here."""

import math

from arena.metrics import derived

# round -> weights for Δlog(1+dragons), Δboard control, Δlongest, Δqueen alive, leader now
WEIGHTS = {
    10: (0.417, 1.531, 0.035, 0.213, 0.210), 20: (1.066, 1.947, 0.080, 0.117, 0.172),
    50: (1.330, 2.178, 0.112, 0.195, 0.155), 100: (1.045, 1.892, 0.098, 0.301, 0.109),
    150: (0.907, 1.436, 0.047, 0.370, 0.299), 200: (0.766, 1.387, 0.029, 0.750, 0.255),
    250: (0.622, 1.309, 0.020, 0.818, 0.384), 300: (0.490, 1.402, 0.021, 1.194, 0.339),
    350: (0.250, 1.943, 0.022, 1.569, 0.336), 400: (0.079, 2.030, 0.030, 1.880, 0.583),
    450: (-0.150, 1.989, 0.037, 2.243, 1.102), 490: (-0.164, 1.574, 0.045, 3.455, 2.166),
}
_KNOTS = sorted(WEIGHTS)


def _weights(rnd):
    if rnd <= _KNOTS[0]:
        return WEIGHTS[_KNOTS[0]]
    if rnd >= _KNOTS[-1]:
        return WEIGHTS[_KNOTS[-1]]
    hi = next(k for k in _KNOTS if k >= rnd)
    lo = max(k for k in _KNOTS if k <= rnd)
    if hi == lo:
        return WEIGHTS[lo]
    f = (rnd - lo) / (hi - lo)
    return tuple(a + f * (b - a) for a, b in zip(WEIGHTS[lo], WEIGHTS[hi]))


def _leader(v):
    """+1 if A leads under the round-500 rule now (queen, then longest, then total), -1 if B."""
    for k in ("queen_length", "longest", "total_length"):
        a, b = v[k]["A"] or 0, v[k]["B"] or 0
        if a != b:
            return 1 if a > b else -1
    return 0


@derived("eval", "Eval: P(A wins)", "chance team A wins, from dragons alive, board control, "
         "longest dragon, queen alive and who leads under the round-500 rule", version=2,
         unit="probability", fitted="2026-10-02T04:40:00Z")
def eval_bar(values, rnd):
    if rnd >= 500:                     # the game is over: the result, not a prediction
        lead = _leader(values)
        return 1.0 if lead > 0 else 0.0 if lead < 0 else 0.5
    w = _weights(rnd)
    d = lambda k: (values[k]["A"] or 0) - (values[k]["B"] or 0)
    z = (w[0] * d("log_dragons") + w[1] * d("board_control") + w[2] * d("longest")
         + w[3] * d("queen_alive") + w[4] * _leader(values))
    if values["dragons"]["A"] == 0 or values["dragons"]["B"] == 0:      # someone's eliminated
        return 1.0 if values["dragons"]["B"] == 0 and values["dragons"]["A"] else 0.0
    return round(1 / (1 + math.exp(-z)), 4)
