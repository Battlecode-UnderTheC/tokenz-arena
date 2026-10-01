"""Picks the next games. Priority: drafts > placement > holdout > continuous.

Drafts: parent and top 3, public maps, both seats. Placement: parent, then every active
bot's current submission. Fair share between owners; a per-owner cap on queued drafts.
"""

# TODO(build order 3): enqueue(submission), next_game() -> queue row
