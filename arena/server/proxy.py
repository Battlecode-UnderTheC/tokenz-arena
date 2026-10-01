"""`/unswbc/api/v1/*`: the competition server's API (https://game.battlecode.au/docs/api),
served by the arena so teammates can point the toolkit at it (`UNSWBC_SERVER`).

- POST /submissions          -> an arena submission (name = bot, description = note, with
                               optional leading "parent=<id>;" and "source=<url>;"); replies in the toolkit's
                               shape {version, status}
- POST /submissions/:id/activate -> 403: only the bridge changes the live bot
- POST /battles              -> forwarded; the remaining hourly quota goes in a header
- every GET                  -> forwarded with the team key, cached (leaderboard/ratings 60 s,
                               battles 15 s, finished battles and replays forever); on 429 the
                               cached copy is served, marked stale

Incoming: no credentials; the caller is known from Tailscale (who.py), and any bearer value
is only an agent label. Outgoing: the team key, read from outside data_dir and
never logged. Replay downloads follow the signed redirect without forwarding the key.
"""

# TODO(build order 9)
