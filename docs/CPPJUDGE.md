# cppjudge: the official judge's games, hosted in C++

`unswbc run --sandbox` spends most of a game in Python: framing each bot's turn, its WASI calls,
the engine's host calls. cppjudge (`judge/`) is a port of that host (unswbc 1.2.7's `run.py`,
`sandbox.py`, `turn.py`, `bot.py`, `engine.py`) to C++ against the same libwasmtime. Everything
that decides a game is the official judge's own files, not a copy:

- the bots: the metered `.wasm` that unswbc's judge clang builds and unswbc's metering prices
  (one shared cache), so the bots' decisions and every turn's CPU points are the official ones;
- the rules: unswbc's `unswbc_engine.wasm`.

So a game played with cppjudge is the official game: the same replay and the same verbose log,
byte for byte (judge/parity.py checks exactly that), in about 1.6x less CPU on heavy bots and up
to 7x on light ones. A bot over the 100M-point cap dies there as with `arena/strictjudge.py`
(`--no-retry`).

## How games use it

`[play] judge = "cpp"` in arena.toml. `arena/runner.py` then plays through `judge/unswbc-cpp`,
which hands a game to the official judge (and says so in the log) whenever cppjudge can't play
it exactly: a Python bot, a bot importing something the host lacks, no cppjudge binary (a Mac
worker), or **an unswbc whose ported files changed** (`PORTED` in the wrapper lists their
hashes; until it's checked against the new version and updated, every game is official).

Each game records which judge played it (`meta.judge`). One game in `judge_check_every`
(default 50) is also played by the official judge and the two replays compared
(`meta.judge_check`); when they disagree, the official result counts. The tally is on the Queue
page and at `/api/v1/judge-checks`; any disagreement means cppjudge needs a look.

## Building

Both images build it in a stage of their own (`Dockerfile`, `docker/worker.Dockerfile`): g++
and the wasmtime C API of the wheel's version, then only the binary is kept; it loads the
wheel's own `_libwasmtime.so`. Locally: `judge/build.sh`.
