"""`unswbc run`, with one change: a bot whose worker dies during its turn dies, no second try.

unswbc's `TurnBot.ask` replaces a worker that exits (or runs out of CPU points) before writing
any of its reply, and asks the same turn again; only a second failure kills the dragon. The
competition server ends it there: a bot that goes over 100M points dies of "no valid action".
This runs the official CLI with `ask` replaced by the same code minus that retry, so the
arena's verdicts follow the server. A worker that died *between* turns is still replaced
before the turn, as unswbc does.

    python -m arena.strictjudge run MAP A B --sandbox --seed S -v -o OUT

`ask` is checked against the source it was written from (`ASK_SHA`): a new unswbc with a
different `ask` refuses to run until this file is updated, rather than quietly playing by
other rules.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
import time

ASK_SHA = "659b6e1d917991a742ad0d3243da1fc8ebd4beeed17628d939808adb4174b6d4"   # unswbc 1.2.7


def strict_ask(self, block: bytes) -> bytes:
    """`TurnBot.ask` (unswbc 1.2.7) without the retry after an exit."""
    from unswbc.turn import WALL_LIMIT_S, payload
    self.error = None
    if not self._running():
        self._fresh()
    for attempt in (0, 1):
        self._framer.arm()
        data = payload((self._init if self._new else b"") + block, self._activate)
        if not self._write(data):                  # died between turns: replaced, asked once
            if attempt == 0:
                self._fresh()
                continue
            return self._skip(self._reason())
        self._written += len(data)
        self._activate = self._new = False
        deadline = time.monotonic() + WALL_LIMIT_S
        while True:
            state = self._poll(max(0.0, deadline - time.monotonic()))
            if self._framer.done or self._framer.park_at == self._written or state == "park":
                return self._framer.take()
            if state == "exit":
                break
            if time.monotonic() >= deadline:
                self._kill()
                return self._skip("ran out of time")
        return self._skip(self._reason())           # died in its turn: no second try
    return b""


def ask_sha() -> str:
    from unswbc.turn import TurnBot
    return hashlib.sha256(inspect.getsource(TurnBot.ask).encode()).hexdigest()


def patch() -> None:
    from unswbc.turn import TurnBot
    got = ask_sha()
    if got != ASK_SHA:
        raise SystemExit(f"arena.strictjudge: unswbc's TurnBot.ask changed (sha {got[:12]}); "
                         "check its retry rule and update strict_ask and ASK_SHA")
    TurnBot.ask = strict_ask


def main(argv: list[str] | None = None) -> int:
    patch()
    from unswbc.cli import main as unswbc_main
    return unswbc_main(argv)


if __name__ == "__main__":
    sys.exit(main())
