"""The competition server, for the bridge and the /api/v1/server/* endpoints (DESIGN.md
section 8): the documented key API (https://game.battlecode.au/docs/api) plus the `bcweb`
package for what only the website has (quota, per-map battle detail, brackets).

Every read is cached (leaderboard/ratings 60 s, battles/quota 15 s, finished battles,
replays and past tournaments forever); on 429 the cached copy is returned marked stale.
Replay downloads follow the signed redirect without forwarding the key.
"""

# TODO(build order 9)
