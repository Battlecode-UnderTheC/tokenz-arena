"""Bradley-Terry ratings over ranked games among current submissions.

A draw counts as half a win; Elo-like scale with the reference bot fixed at 1000; refit in full
after each batch, with a confidence interval per submission.
"""

# TODO(build order 4): fit(games) -> {submission: (elo, err)}
