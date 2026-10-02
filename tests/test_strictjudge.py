"""arena.strictjudge: a bot that dies in its turn isn't asked again."""

import pytest

from arena import strictjudge
from unswbc.turn import TurnBot


class Fake(TurnBot):
    """A worker that exits during every turn, before saying anything."""

    def __init__(self):
        super().__init__(init=b"ID 1\n")
        self.starts = 0
        self.alive = False

    def _running(self):
        return self.alive

    def start(self):
        self.starts += 1
        self.alive = True

    def stop(self):
        self.alive = False

    _kill = stop

    def _write(self, data):
        return True

    def _poll(self, timeout):
        self.alive = False
        return "exit"

    def _reason(self):
        return "exceeded CPU limit"


def test_unswbc_retries_but_strict_does_not():
    loose = Fake()
    assert TurnBot.ask(loose, b"ROUND 1\n") == b""
    assert loose.starts == 2                         # unswbc: a fresh worker, asked again
    strict = Fake()
    assert strictjudge.strict_ask(strict, b"ROUND 1\n") == b""
    assert strict.starts == 1 and strict.error == "exceeded CPU limit"


def test_refuses_a_changed_unswbc(monkeypatch):
    monkeypatch.setattr(strictjudge, "ASK_SHA", "0" * 64)
    with pytest.raises(SystemExit):
        strictjudge.patch()


def test_written_for_this_unswbc():
    assert strictjudge.ask_sha() == strictjudge.ASK_SHA


def test_overruns_are_counted():
    from arena.runner import parse
    from arena.stats import extract
    log = "\n".join([
        "seed 0x0000000000000001",
        "round 440: bot 1 (team B) stdout:", "MOVE N", "round 440: bot 1 (team B) points 64668222 memory 589824",
        "round 441: bot 1 (team B) exceeded CPU limit",
        "round 441: bot 1 (team B) died: no valid action",
        "team B wins after 460 rounds (by elimination) (1m02s)"])
    r = parse(log)
    r.log = log
    t = extract(r)["teams"]
    assert t["B"]["cpu_overruns"] == 1 and t["A"]["cpu_overruns"] == 0
    assert t["B"]["points_max"] == 64668222
