"""One game with the official judge: `unswbc run <map> A B --sandbox --seed S`.

Each game gets fresh copies of both bot directories (the toolkit builds inside them), so
parallel games never share build output. The result is parsed from the judge's stdout:

    round 67: bot 1 (team B) died: hit a wall
    team A wins after 68 rounds (by elimination) (1.8s)
    team A points per turn: p50 5.2M  p99 20.4M  mean 5.4M  max 20.4M  (68 turns)
    wrote replay: out/a-vs-b-on-Colosseum-2026-10-01-202847.replay
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

_WIN = re.compile(r"^team ([AB]) wins after (\d+) rounds \(by ([^)]*)\)", re.M)
_DRAW = re.compile(r"^(?:draw|it's a draw|tie)\b.*?after (\d+) rounds(?: \(by ([^)]*)\))?", re.M | re.I)
_DEATH = re.compile(r"^round (\d+): bot (\d+) \(team ([AB])\) died: (.+)$", re.M)
_POINTS = re.compile(r"^team ([AB]) points per turn: p50 ([\d.]+)([KMG]?)\s+p99 ([\d.]+)([KMG]?)\s+"
                     r"mean ([\d.]+)([KMG]?)\s+max ([\d.]+)([KMG]?)", re.M)
_REPLAY = re.compile(r"^wrote replay: (.+)$", re.M)
_SCALE = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9}


@dataclass
class Death:
    round: int
    bot: int
    team: str
    cause: str


@dataclass
class GameResult:
    winner: str | None          # "A", "B", or None for a draw
    rounds: int
    reason: str                 # "elimination", the round-500 tiebreak, ...
    deaths: list[Death] = field(default_factory=list)
    points: dict[str, dict[str, float]] = field(default_factory=dict)   # team -> p50/p99/mean/max
    replay: Path | None = None
    seconds: float = 0.0
    log: str = ""               # full stdout+stderr, for stats.py and debugging


class JudgeError(RuntimeError):
    """The judge ran but its output had no verdict (a build failure, a crash)."""

    def __init__(self, message: str, log: str):
        tail = "\n".join(log.strip().splitlines()[-5:])
        super().__init__(f"{message}:\n{tail}")
        self.log = log


def _num(value: str, unit: str) -> float:
    return float(value) * _SCALE[unit]


def parse(log: str) -> GameResult:
    if m := _WIN.search(log):
        winner, rounds, reason = m.group(1), int(m.group(2)), m.group(3)
    elif m := _DRAW.search(log):
        winner, rounds, reason = None, int(m.group(1)), m.group(2) or "draw"
    else:
        raise JudgeError("no verdict in judge output", log)
    result = GameResult(winner=winner, rounds=rounds, reason=reason, log=log)
    result.deaths = [Death(int(r), int(b), t, c.strip()) for r, b, t, c in _DEATH.findall(log)]
    for team, *v in _POINTS.findall(log):
        result.points[team] = {"p50": _num(v[0], v[1]), "p99": _num(v[2], v[3]),
                               "mean": _num(v[4], v[5]), "max": _num(v[6], v[7])}
    if m := _REPLAY.search(log):
        result.replay = Path(m.group(1).strip())
    return result


def play(map_path: Path, bot_a: Path, bot_b: Path, seed: int, replay_dir: Path,
         work_root: Path | None = None, unswbc: str = "unswbc", timeout: float = 1800) -> GameResult:
    """Plays bot_a (team A) against bot_b (team B). Raises JudgeError when there's no verdict."""
    map_path, replay_dir = map_path.resolve(), replay_dir.resolve()
    replay_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="game-", dir=work_root) as work:
        a, b = Path(work, "A"), Path(work, "B")
        shutil.copytree(bot_a, a)
        shutil.copytree(bot_b, b)
        cmd = [unswbc, "run", str(map_path), str(a), str(b), "--sandbox", "--seed", str(seed),
               "-v", "-o", str(replay_dir)]
        start = time.monotonic()
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
        log = proc.stdout + proc.stderr
        result = parse(log)
        result.seconds = time.monotonic() - start
        if result.replay and not result.replay.is_absolute():
            result.replay = Path(work, result.replay)
        return result
