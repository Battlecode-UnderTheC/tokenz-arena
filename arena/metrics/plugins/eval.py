"""The eval bar: P(team A wins) at each round, from a few metrics (see the analysis in
docs/METRICS.md). Weights were fitted per round on ~8,500 arena games under unswbc 1.2.3 and
checked on real competition games (AUC 0.84 at round 100, 0.97 at round 490)."""

import math

from arena.metrics import derived

# round -> weights for Δlog(1+dragons), Δboard control, Δlongest, Δqueen alive, leader now
WEIGHTS = {
    10: (1.383, 2.341, 0.020, -0.274, 0.000), 20: (1.198, 1.249, 0.082, -0.298, -0.011),
    50: (1.941, 1.577, 0.129, 0.146, -0.189), 100: (1.435, 1.532, 0.008, 0.247, 0.060),
    150: (1.446, 0.869, -0.021, 0.222, 0.055), 200: (1.580, 0.429, -0.005, 0.534, -0.057),
    250: (1.456, 0.237, 0.024, 0.574, 0.016), 300: (1.145, 0.806, 0.034, 0.857, -0.055),
    350: (0.837, 0.868, 0.054, 0.904, 0.100), 400: (0.361, 1.366, 0.080, 1.083, 0.273),
    450: (0.087, 1.249, 0.116, 1.837, 0.448), 490: (-0.071, 0.812, 0.245, 6.148, 0.878),
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
         "longest dragon, queen alive and who leads under the round-500 rule", version=1,
         unit="probability")
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
