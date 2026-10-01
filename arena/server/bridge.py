"""The server bridge (DESIGN.md section 8): keeps the team's live submission on the
competition server in step with the arena, spends the bridge's share of the battle quota,
and imports every server game as an arena game with suite = 'server'.

Steps, each a function the arena process calls on its own timer:
    poll()      fetch our new battles and their replays, import them (read-only; build first)
    gate()      is there a candidate? returns it with the evidence, or why not
    submit()    `unswbc submit` the candidate (mode auto, or approved on the website)
    spend()     request battles within quota_share, per the plan in DESIGN.md

Website access goes through bcweb.py (credentials in ~/.unswbc-web/, never printed or
copied). Uploads go through `unswbc submit` (the team key in ~/.unswbc/keys.json, never read
here). Every action is appended to data_dir/server/log.jsonl.
"""

# TODO(build order 9). bcweb.py is a verbatim copy of syeyoung-tokens scripts/server/bcweb.py;
# its replay paths (LEAGUE_ROOT, DECODE_DIRS) still point at syeyoung-tokens.league and must
# move to data_dir/server/ before use.
