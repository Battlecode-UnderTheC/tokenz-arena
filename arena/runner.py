"""One game with the official judge: `unswbc run <map> A B --sandbox --seed S`.

Each game gets fresh copies of both bot directories (the toolkit builds inside them), so
parallel games never share build output. The result is parsed from the judge's stdout:

    round 67: bot 1 (team B) died: hit a wall
    team A wins after 68 rounds (by elimination) (1.8s)
    team A points per turn: p50 5.2M  p99 20.4M  mean 5.4M  max 20.4M  (68 turns)
    wrote replay: out/a-vs-b-on-Colosseum-2026-10-01-202847.replay
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_WIN = re.compile(r"^team ([AB]) wins after (\d+) rounds \((?:by )?([^)]*)\)", re.M)
_DRAW = re.compile(r"^(?:draw|it's a draw|tie)\b.*?after (\d+) rounds(?: \(by ([^)]*)\))?", re.M | re.I)
_DEATH = re.compile(r"^round (\d+): bot (\d+) \(team ([AB])\) died: (.+)$", re.M)
_POINTS = re.compile(r"^team ([AB]) points per turn: p50 ([\d.]+)([KMG]?)\s+p99 ([\d.]+)([KMG]?)\s+"
                     r"mean ([\d.]+)([KMG]?)\s+max ([\d.]+)([KMG]?)", re.M)
_REPLAY = re.compile(r"^wrote replay: (.+)$", re.M)
_SCALE = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")   # colour codes the toolkit may add


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
    judge: str = "official"     # which judge played it: "official" or "cpp" (judge/unswbc-cpp)


class JudgeError(RuntimeError):
    """The judge ran but its output had no verdict (a build failure, a crash)."""

    def __init__(self, message: str, log: str):
        tail = "\n".join(log.strip().splitlines()[-5:])
        super().__init__(f"{message}:\n{tail}")
        self.log = log


def _num(value: str, unit: str) -> float:
    return float(value) * _SCALE[unit]


def parse(log: str) -> GameResult:
    log = _ANSI.sub("", log)
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


def wasm_path(bot: Path) -> Path | None:
    """Where the judge caches this bot's wasm, or None for a bot without C/C++ sources. The name
    is the folder name plus a hash of the judge's flags and the sources, computed exactly as
    unswbc's clangtool.build does, so it's the same on every machine with this unswbc: a worker
    can download the engine's build and the judge finds it in its cache."""
    from unswbc import clangtool
    from unswbc.sandbox import _cache_dir
    bot = Path(bot)
    found = clangtool.sources(bot)
    if not found:
        return None
    stamp = hashlib.sha256(
        repr([clangtool.DRIVER_FLAGS, clangtool.LINK_FLAGS, clangtool.LINK_TAIL,
              [p.as_posix() for p in found]]).encode()
        + b"".join((bot / p).read_bytes() for p in found)).hexdigest()[:32]
    return _cache_dir() / "wasmbots" / f"{bot.resolve().name}-{stamp}.wasm"


def build_wasm(bot: Path) -> Path | None:
    """Builds a C/C++ bot's wasm with the judge's own clang, once per machine: under a file lock
    per bot, so parallel games (or downloads) wait for one build instead of each compiling it.
    Returns the cached file (None for a Python bot)."""
    import fcntl

    from unswbc import clangtool
    from unswbc.sandbox import _cache_dir
    bot = Path(bot)
    if not clangtool.sources(bot):
        return None
    locks = _cache_dir() / "wasmbots"
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / f".{bot.resolve().name}.lock", "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)                # the first builds; the rest wait for it
        return Path(clangtool.build(bot))            # returns at once when it's already cached


def prebuild(bot: Path) -> None:
    """build_wasm() before a game; any problem is left to the game itself to report."""
    try:
        build_wasm(bot)
    except Exception as e:
        import logging
        logging.getLogger("arena").warning("prebuild of %s: %s: %s (the game will build it)",
                                           Path(bot).name, type(e).__name__, e)


# cppjudge (judge/): the same metered wasm bots and engine, hosted in C++ instead of Python.
# Its wrapper plays a game with it when it can, and with the official judge otherwise (and says so).
CPP_WRAPPERS = [ROOT / "judge" / "unswbc-cpp", Path("/opt/cppjudge/unswbc-cpp")]
CPP_FALLBACK = "unswbc-cpp: official judge"


def cpp_wrapper() -> Path | None:
    return next((w for w in CPP_WRAPPERS if w.is_file()), None)


def play(map_path: Path, bot_a: Path, bot_b: Path, seed: int, replay_dir: Path,
         work_root: Path | None = None, unswbc: str = "unswbc", timeout: float = 1800,
         judge: str = "official") -> GameResult:
    """Plays bot_a (team A) against bot_b (team B). Raises JudgeError when there's no verdict.

    Each bot is copied to <work>/A/<its directory name> and <work>/B/<name>: the judge's wasm
    build cache is keyed by directory name plus source hash, so keeping the name (the
    submission id) means a C/C++ bot compiles once, not once per game (60 s vs 10 s).
    """
    map_path, replay_dir = map_path.resolve(), replay_dir.resolve()
    replay_dir.mkdir(parents=True, exist_ok=True)
    for bot in (bot_a, bot_b):
        prebuild(Path(bot))
    if work_root:
        work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="game-", dir=work_root) as work:
        a = Path(work, "A", Path(bot_a).name)
        b = Path(work, "B", Path(bot_b).name)
        shutil.copytree(bot_a, a)
        shutil.copytree(bot_b, b)
        out = Path(work, "replay")
        # unswbc's CLI, except that a bot dying in its turn isn't asked again (arena/strictjudge.py)
        wrapper = cpp_wrapper() if judge == "cpp" and unswbc == "unswbc" else None
        if wrapper:
            exe = [sys.executable, str(wrapper)]
        else:
            exe = [sys.executable, "-m", "arena.strictjudge"] if unswbc == "unswbc" else [unswbc]
        cmd = [*exe, "run", str(map_path), str(a), str(b), "--sandbox", "--seed", str(seed),
               "-v", "-o", str(out)]
        if wrapper:
            cmd.append("--no-retry")
        start = time.monotonic()
        env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}   # plain output to parse
        root = str(Path(__file__).resolve().parent.parent)          # arena.strictjudge, from cwd=work
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
        try:
            proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout,
                                  errors="replace", env=env)
        except subprocess.TimeoutExpired as e:
            raise JudgeError(f"judge timed out after {timeout:.0f}s", str(e.stdout or "")) from e
        log = proc.stdout + proc.stderr
        result = parse(log)
        result.seconds = time.monotonic() - start
        result.judge = "cpp" if wrapper and CPP_FALLBACK not in log else "official"
        replays = sorted(out.glob("*.replay"))
        if replays:
            dest = replay_dir / replays[0].name
            shutil.move(str(replays[0]), dest)
            result.replay = dest
        else:
            result.replay = None
        return result
