"""runner.play(judge="cpp"): the cppjudge wrapper with the arena's rules, and which judge played."""

import subprocess
from pathlib import Path

from arena import runner

WIN = "team A wins after 12 rounds (by elimination) (0.1s)\n"


def fake_run(log, seen):
    def run(cmd, cwd=None, **kw):
        seen.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "x.replay").write_bytes(b"r")
        return subprocess.CompletedProcess(cmd, 0, log, "")
    return run


def game(tmp_path, monkeypatch, log, judge):
    seen = []
    monkeypatch.setattr(runner, "prebuild", lambda bot: None)
    monkeypatch.setattr(runner.subprocess, "run", fake_run(log, seen))
    for t in "ab":
        (tmp_path / t).mkdir()
    r = runner.play(tmp_path / "m.map", tmp_path / "a", tmp_path / "b", 1, tmp_path / "replays",
                    work_root=tmp_path / "work", judge=judge)
    return r, seen[0]


def test_cpp_judge_plays_with_no_retry(tmp_path, monkeypatch):
    r, cmd = game(tmp_path, monkeypatch, WIN, "cpp")
    assert cmd[1].endswith("unswbc-cpp") and cmd[-1] == "--no-retry"
    assert r.judge == "cpp" and r.winner == "A"


def test_cpp_fallback_is_recorded_as_official(tmp_path, monkeypatch):
    r, _ = game(tmp_path, monkeypatch, "unswbc-cpp: official judge (no cppjudge binary)\n" + WIN, "cpp")
    assert r.judge == "official"


def test_official_judge_is_the_strict_one(tmp_path, monkeypatch):
    r, cmd = game(tmp_path, monkeypatch, WIN, "official")
    assert cmd[1:3] == ["-m", "arena.strictjudge"] and r.judge == "official"


def checked(tmp_path, monkeypatch, cpp_replay, official_replay):
    """A spot-checked game: the first run is cppjudge's, the second the official judge's."""
    seen = []
    logs = [WIN, "team B wins after 30 rounds (by elimination) (0.1s)\n"]
    replays = [cpp_replay, official_replay]

    def run(cmd, cwd=None, **kw):
        seen.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "x.replay").write_bytes(replays[len(seen) - 1])
        return subprocess.CompletedProcess(cmd, 0, logs[len(seen) - 1], "")
    monkeypatch.setattr(runner, "prebuild", lambda bot: None)
    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner, "_events", lambda p: p.read_bytes())
    for t in "ab":
        (tmp_path / t).mkdir()
    r = runner.play(tmp_path / "m.map", tmp_path / "a", tmp_path / "b", 1, tmp_path / "replays",
                    work_root=tmp_path / "work", judge="cpp", check=True)
    return r, seen


def test_spot_check_agrees(tmp_path, monkeypatch):
    r, seen = checked(tmp_path, monkeypatch, b"same", b"same")
    assert len(seen) == 2 and seen[1][1:3] == ["-m", "arena.strictjudge"]
    assert r.check == "same" and r.judge == "cpp" and r.winner == "A"


def test_spot_check_disagreement_counts_the_official_result(tmp_path, monkeypatch):
    r, _ = checked(tmp_path, monkeypatch, b"cpp", b"official")
    assert r.check == "different" and r.judge == "official" and r.winner == "B"
    assert r.replay.read_bytes() == b"official"


def test_spot_check_picks_one_in_every():
    from arena.config import Config
    from arena.judge import spot_check
    cfg = Config(raw={"play": {"judge": "cpp", "judge_check_every": 10}})
    picked = sum(spot_check(cfg, f"g{i}") for i in range(5000))
    assert 400 < picked < 600
    assert not spot_check(Config(raw={"play": {"judge": "official"}}), "g1")
