"""The server bridge (DESIGN.md section 8), on the competition server's key API.

    download()  poll GET /battles each minute; for each finished game fetch the replay (follow
                the signed redirect, don't forward the key) and the battle log; import it as an
                arena game, suite 'server', vs external bot server:<team>; add its map and seed
                to the server suite
    gate()      the champion gate: returns the candidate with its evidence, or why there is none
    submit()    POST /submissions then /activate for the candidate; at most every min_hours,
                unless paused

Every action is appended to data_dir/server/log.jsonl.
"""

# TODO(build order 9)
