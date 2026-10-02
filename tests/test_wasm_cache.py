"""Prebuilt bots: the judge's wasm cache name computed here matches unswbc's own build; the
engine serves a bot's build with that name; a worker drops it into its cache; builds of one bot
on one machine happen once, under a lock."""

import io
import threading
from pathlib import Path

import pytest
from unswbc import clangtool

from arena import runner


@pytest.fixture
def fake_clang(tmp_path, monkeypatch):
    """unswbc's build with the slow parts (preparing clang, compiling) stubbed out."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    calls = []

    def step(wasm, home, argv, mounts, what):
        calls.append(what)
        if argv[0] == "/bin/wasm-ld":
            (mounts["/out"] / "bot.wasm").write_bytes(b"\0asm-fake")
    monkeypatch.setattr(clangtool, "_step", step)
    monkeypatch.setattr(clangtool, "warm", lambda: True)
    monkeypatch.setattr(clangtool, "toolchain", lambda: tmp_path)
    return calls


def bot(tmp_path, name="cxx-1", text="int main(){}"):
    d = tmp_path / "bots" / name
    d.mkdir(parents=True)
    (d / "bot.toml").write_text('[project]\nlanguage = "c++"\n')
    (d / "main.cpp").write_text(text)
    return d


def test_cache_name_matches_unswbc(fake_clang, tmp_path):
    d = bot(tmp_path)
    expected = runner.wasm_path(d)
    built = clangtool.build(d)
    assert Path(built) == expected
    other = bot(tmp_path, "cxx-2", "int main(){return 1;}")
    assert runner.wasm_path(other).name != expected.name.replace("cxx-1", "cxx-2")   # source counts
    py = tmp_path / "bots" / "py-1"
    py.mkdir(parents=True)
    (py / "main.py").write_text("")
    assert runner.wasm_path(py) is None and runner.build_wasm(py) is None


def test_parallel_builds_of_one_bot_happen_once(fake_clang, tmp_path):
    d = bot(tmp_path)
    ts = [threading.Thread(target=runner.build_wasm, args=(d,)) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for c in fake_clang if c.startswith("linking")) == 1


def test_worker_takes_the_engines_build(fake_clang, tmp_path, monkeypatch):
    from arena.worker import Worker
    d = bot(tmp_path)
    name = runner.wasm_path(d).name

    class Resp(io.BytesIO):
        headers = {"X-Wasm-Name": name}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: Resp(b"\0asm-engine"))
    w = Worker("http://arena", "t", "w", 1, tmp_path / "cache")
    w.wasm("cxx-1", d)
    assert runner.wasm_path(d).read_bytes() == b"\0asm-engine"
    clangtool.build(d)                                   # the judge now finds it: no compiling
    assert not any(c.startswith("compiling") for c in fake_clang)
    w.procs.shutdown()


def test_engine_serves_builds_to_workers(fake_clang, tmp_path):
    from fastapi.testclient import TestClient

    from arena import config, workers
    from arena.api.http import create_app
    from arena.daemon import Daemon
    from arena.state import Arena
    cfg = config.Config(raw={"play": {"workers": 0}, "workers": {"tokens": {"lab": "s3cret"}}},
                        data_dir=tmp_path / "d")
    a = Arena(cfg)
    workers.install(a, Daemon(a))
    bot(cfg.data_dir, "cxx-9")                          # data_dir/bots/cxx-9
    c = TestClient(create_app(a))
    assert c.get("/api/v1/workers/wasm/cxx-9").status_code == 401
    r = c.get("/api/v1/workers/wasm/cxx-9", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.content == b"\0asm-fake"
    assert r.headers["x-wasm-name"] == runner.wasm_path(cfg.bots / "cxx-9").name
    assert c.get("/api/v1/workers/wasm/nope", headers={"Authorization": "Bearer s3cret"}).status_code == 404


def test_a_submission_compiles_before_it_is_queued(fake_clang, tmp_path, monkeypatch):
    from arena import config, submit
    from arena.state import Arena
    from tests.test_core import make_zip
    cfg = config.Config(raw={"maps": {"worldgen": False}}, data_dir=tmp_path / "d")
    a = Arena(cfg)
    zipped = make_zip({"bot.toml": '[project]\nlanguage = "c++"\n', "main.cpp": "int main(){}"})
    s, _ = submit.accept(a, blob=zipped, bot="cxx", owner="alice")
    order = []
    monkeypatch.setattr(submit.runner, "build_wasm", lambda d: order.append("build") or Path(d))
    monkeypatch.setattr(submit.runner, "play", lambda *x, **k: order.append("check game") or
                        type("R", (), {"winner": "A", "rounds": 9, "reason": "x", "replay": None})())
    assert submit.check(a, s) and order == ["build", "check game"] and s.status == "queued"

    def broken(d):
        raise clangtool.BuildError("compiling main.cpp\nmain.cpp:1:1: error: expected ';'")
    s2, _ = submit.accept(a, blob=make_zip({"bot.toml": '[project]\nlanguage = "c++"\n', "main.cpp": "x"}),
                          bot="cxx2", owner="alice")
    monkeypatch.setattr(submit.runner, "build_wasm", broken)
    assert not submit.check(a, s2)
    assert s2.status == "rejected" and "didn't compile" in s2.reason and "expected ';'" in s2.reason
