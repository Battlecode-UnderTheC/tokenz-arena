"""Per-game stats from the judge's verbose log (runner.GameResult.log), stored in games/<id>.json.

The verbose log (`unswbc run -v`, 1.2.3) has per-turn lines we count:

    round 12: bot 3 (team A) stdout:          then the bot's commands (MOVE E, SPLIT 4, SONAR N 3)
    round 12: bot 3 (team A) points 4512345 memory 1234
    round 67: bot 1 (team B) died: hit a wall
    round 441: bot 1 (team B) exceeded CPU limit       (then "died: no valid action")
    running round 51/500 --  last: 0ms  eta: 16.2s  dragons: 3 vs 4
    team A wins after 500 rounds (longest dragon, 45 to 34) (11.1s)

It's about 100k lines for a 500-round game, so it's parsed here and thrown away.
"""

from __future__ import annotations

import re

from arena.runner import GameResult

_STDOUT = re.compile(r"^round (\d+): bot (\d+) \(team ([AB])\) stdout:")
_POINTS = re.compile(r"^round \d+: bot \d+ \(team ([AB])\) points (\d+)")
_OVERRUN = re.compile(r"^round \d+: bot \d+ \(team ([AB])\) exceeded CPU limit")
_PROGRESS = re.compile(r"^running round (\d+)/\d+ .*dragons: (\d+) vs (\d+)")
_TIEBREAK = re.compile(r"^(.*?),\s*(\d+) to (\d+)$")
_ERROR = re.compile(r"(timed out|time limit|crash|exception|out of memory|memory limit|"
                    r"no output|invalid command|unreadable)", re.I)


def extract(result: GameResult) -> dict:
    teams = {t: {"deaths": {}, "bots": set(), "turns": 0, "moves": 0, "sprints": 0, "splits": 0,
                 "sonars": 0, "errors": 0, "points_max": 0, "cpu_overruns": 0} for t in "AB"}
    series = []          # [round, dragons A, dragons B] every 10 rounds
    team = None
    for line in result.log.splitlines():
        if m := _STDOUT.match(line):
            team = m.group(3)
            teams[team]["bots"].add(int(m.group(2)))
            teams[team]["turns"] += 1
            continue
        if line.startswith(("MOVE ", "SPLIT", "SONAR")) and team:
            t = teams[team]
            if line.startswith("MOVE "):
                t["moves"] += 1
                if len(line.split()[1]) > 1:
                    t["sprints"] += 1
            elif line.startswith("SPLIT"):
                t["splits"] += 1
            else:
                t["sonars"] += 1
            continue
        if m := _POINTS.match(line):
            t = teams[m.group(1)]
            t["points_max"] = max(t["points_max"], int(m.group(2)))
            team = None
            continue
        if m := _OVERRUN.match(line):
            teams[m.group(1)]["cpu_overruns"] += 1
            continue
        if m := _PROGRESS.match(line):
            r = int(m.group(1))
            if r % 10 == 0 or r == 1:
                series.append([r, int(m.group(2)), int(m.group(3))])
            continue
        if _ERROR.search(line) and "died:" not in line:
            if t := teams.get(team or ""):
                t["errors"] += 1
    for d in result.deaths:
        dd = teams[d.team]["deaths"]
        dd[d.cause] = dd.get(d.cause, 0) + 1
    out = {"teams": {}, "dragons": series}
    for k, t in teams.items():
        t = dict(t)
        t["dragons_used"] = len(t.pop("bots"))
        t["died"] = sum(t["deaths"].values())
        t["points"] = result.points.get(k, {})
        out["teams"][k] = t
    if result.reason and (m := _TIEBREAK.match(result.reason)):
        out["tiebreak"] = {"by": m.group(1), "winner": int(m.group(2)), "loser": int(m.group(3))}
    return out
