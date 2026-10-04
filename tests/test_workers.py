"""The remote worker protocol, engine side (docs/WORKERS.md)."""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from arena import config, submit, workers
from arena.api.http import create_app
from arena.daemon import Daemon
from arena.state import ENGINE, Arena

from tests.test_core import BOT, make_zip

TOKEN = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def setup(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "one.map").write_text("MAP 4 4\n")
    cfg = config.Config(raw={"maps": {"official": str(maps), "tournament": str(tmp_path / "x"),
                                      "worldgen": False},
                             "play": {"workers": 0, "continuous": False},
                             "workers": {"tokens": {"lab": "s3cret"}}}, data_dir=tmp_path / "d")
    a = Arena(cfg)
    subs = []
    for name in ("p", "q"):
        s, _ = submit.accept(a, blob=make_zip({**BOT, "x": name}), bot=name, owner="alice", lane="ranked")
        (cfg.bots / s.id).mkdir(parents=True)
        submit.make_current(a, s)
        subs.append(s)
    d = Daemon(a)
    workers.install(a, d)
    return a, d, TestClient(create_app(a)), subs


def hello(**k):
    return {"worker": "w1", "version": ENGINE, "slots": 4, **k}


def test_lease_needs_token_and_same_judge(setup):
    a, d, c, _ = setup
    assert c.post("/api/v1/workers/lease", json=hello()).status_code == 401
    r = c.post("/api/v1/workers/lease", json=hello(version="0.0.1"), headers=TOKEN)
    assert r.status_code == 409


def test_lease_play_record_and_no_double_lease(setup):
    a, d, c, (p, q) = setup
    r = c.post("/api/v1/workers/lease", json=hello(want=2), headers=TOKEN).json()
    assert len(r["leases"]) == 2
    g0 = r["leases"][0]["game"]
    assert g0["a"]["sha256"] == a.subs[g0["a"]["id"]].sha256 and g0["map"]["name"] == "one"
    zip_ = c.get(f"/api/v1/workers/bot/{g0['a']['id']}", headers=TOKEN).content
    assert hashlib.sha256(zip_).hexdigest() == g0["a"]["sha256"]
    mp = c.get(f"/api/v1/workers/map/official/one", headers=TOKEN).content
    assert hashlib.sha256(mp).hexdigest() == g0["map"]["sha256"]
    keys = {(x["game"]["a"]["id"], x["game"]["b"]["id"]) for x in r["leases"]}
    again = c.post("/api/v1/workers/lease", json=hello(want=4), headers=TOKEN).json()["leases"]
    assert not keys & {(x["game"]["a"]["id"], x["game"]["b"]["id"]) for x in again}
    meta = {"lease": r["leases"][0]["lease"], "worker": "w1", "winner": "A", "rounds": 77,
            "reason": "elimination", "seconds": 3.0, "stats": {}}
    out = c.post("/api/v1/workers/result", data={"meta": json.dumps(meta)},
                 files={"replay": ("g.replay", b"REPLAY")}, headers=TOKEN).json()
    assert out["recorded"] and a.games[-1].meta["worker"] == "w1" and a.games[-1].replay
    dup = c.post("/api/v1/workers/result", data={"meta": json.dumps(meta)}, headers=TOKEN).json()
    assert dup["recorded"] is False
    listing = c.get("/api/v1/workers").json()
    assert listing["remote"][0]["name"] == "w1" and listing["remote"][0]["games_last_hour"] == 1


def test_expired_lease_is_owed_again(setup):
    a, d, c, _ = setup
    l = c.post("/api/v1/workers/lease", json=hello(want=1), headers=TOKEN).json()["leases"][0]
    key = next(k for k, r in a.running.items() if r.get("lease") == l["lease"])
    a.workers.leases[l["lease"]]["expires"] = time.time() - 1
    a.workers.reap()
    assert key not in a.running
    meta = {"lease": l["lease"], "worker": "w1", "winner": "B", "rounds": 9, "reason": "elimination"}
    late = c.post("/api/v1/workers/result", data={"meta": json.dumps(meta)}, headers=TOKEN).json()
    assert late["recorded"] is False                    # expired: the game will be leased again


def test_holdout_maps_never_served(setup):
    a, d, c, _ = setup
    assert c.get("/api/v1/workers/map/holdout/h1", headers=TOKEN).status_code == 404


def test_worker_exits_when_idle(tmp_path, monkeypatch):
    from arena import worker as wmod
    w = wmod.Worker("http://arena", "t", "w", 1, tmp_path, idle_exit=0.2)
    monkeypatch.setattr(w, "_json", lambda path, payload: {"leases": [], "heartbeat_seconds": 30})
    monkeypatch.setattr(wmod.time, "sleep", lambda s: None)
    t0 = time.monotonic()
    w.last_work -= 1                      # idle for a second already
    w.run()
    assert w.stopping.is_set() and time.monotonic() - t0 < 5


def test_scaler_tokens_work_on_every_route_and_revoked_ones_dont(setup):
    """The routes and the registry share one token check: a rented machine's token from
    worker-tokens.json leases and downloads; once revoked, it's refused everywhere."""
    from arena.workers import tokens_file
    a, d, c, subs = setup
    with tokens_file(a.cfg) as t:
        t["vast-x"] = {"token": "rented-token"}
    h = {"Authorization": "Bearer rented-token"}
    from arena.worker import build_id
    r = c.post("/api/v1/workers/lease", json=hello(worker="vast-x", want=1, build=build_id()), headers=h)
    assert r.status_code == 200 and r.json()["leases"]
    assert c.get(f"/api/v1/workers/bot/{subs[0].id}", headers=h).status_code == 200
    with tokens_file(a.cfg) as t:
        t["vast-x"]["revoked"] = True
    assert c.post("/api/v1/workers/lease", json=hello(worker="vast-x"), headers=h).status_code == 401
    assert c.get(f"/api/v1/workers/bot/{subs[0].id}", headers=h).status_code == 401


def test_remote_results_keep_which_judge_played(setup, tmp_path):
    """A worker's result says which judge played the game and how its spot check went; both are
    recorded (they were once dropped, so every remote game read as the official judge's)."""
    a, d, c, _ = setup
    a.cfg.raw["play"].update({"judge": "cpp", "judge_check_every": 1})
    l = c.post("/api/v1/workers/lease", json=hello(want=1), headers=TOKEN).json()["leases"][0]
    assert l["game"]["judge"] == "cpp" and l["game"]["check"] is True
    meta = {"lease": l["lease"], "worker": "w1", "winner": "A", "rounds": 77, "reason": "elimination",
            "seconds": 3.0, "stats": {}, "judge": "cpp", "judge_check": "same"}
    assert c.post("/api/v1/workers/result", data={"meta": json.dumps(meta)}, headers=TOKEN).json()["recorded"]
    g = a.games[-1]
    assert g.meta["judge"] == "cpp" and g.meta["judge_check"] == "same"
    bad = {**meta, "judge": "native"}
    assert c.post("/api/v1/workers/result", data={"meta": json.dumps(bad)}, headers=TOKEN).status_code == 400


def test_rented_workers_on_older_code_drain_one_at_a_time(setup):
    from arena.worker import build_id
    a, d, c, _ = setup
    (a.cfg.data_dir / "worker-image.build").write_text(build_id() + "\n")   # the pushed image
    lease = lambda name, **k: c.post("/api/v1/workers/lease", json=hello(worker=name, want=1, **k),
                                     headers=TOKEN).json()["leases"]
    assert lease("vast-old1") == []                       # no build: older code, drained first
    assert lease("vast-old2")                             # waits its turn: still leased
    assert lease("vast-new", build=build_id())            # current code
    lease("lab-box")                                      # not a rented machine: never drained
    rows = {w["name"]: w for w in c.get("/api/v1/workers").json()["remote"]}
    assert rows["vast-old1"]["draining"] and rows["vast-old2"]["outdated"] and not rows["vast-old2"]["draining"]
    assert not rows["vast-new"]["outdated"] and not rows["lab-box"]["outdated"]
    a.workers.workers.pop("vast-old1")                    # returned by the scaler
    assert lease("vast-old2") == []                       # now it drains


def test_nothing_drains_until_a_pushed_image_is_recorded(setup):
    a, d, c, _ = setup
    r = c.post("/api/v1/workers/lease", json=hello(worker="vast-old", want=1), headers=TOKEN).json()
    assert r["leases"]                                    # no worker-image.build: never drained


def test_a_machine_the_scaler_marks_is_drained(setup):
    from arena import store
    from arena.worker import build_id
    a, d, c, _ = setup
    store.write_json(a.cfg.data_dir / "scaler" / "state.json", {"instances": {"vast-small": {"replace": "resize"}}})
    r = c.post("/api/v1/workers/lease", json=hello(worker="vast-small", want=1, build=build_id()), headers=TOKEN)
    assert r.json()["leases"] == []


def test_a_returned_machines_games_are_owed_again_at_once(setup):
    from arena.workers import tokens_file
    a, d, c, _ = setup
    with tokens_file(a.cfg) as t:
        t["vast-r"] = {"token": "rt"}
    h = {"Authorization": "Bearer rt"}
    got = c.post("/api/v1/workers/lease", json=hello(worker="vast-r", want=2), headers=h).json()["leases"]
    assert got and len(a.workers.leases) == len(got)
    with tokens_file(a.cfg) as t:
        t["vast-r"]["revoked"] = True                     # the scaler returned it
    a.workers.reap()
    assert not a.workers.leases and "vast-r" not in a.workers.workers
    again = c.post("/api/v1/workers/lease", json=hello(want=2), headers=TOKEN).json()["leases"]
    assert len(again) == len(got)


def test_a_machine_failure_is_played_again_and_final_after_three(setup):
    a, d, c, _ = setup
    err = {"error": "worker vast-z: RuntimeError: cannot schedule new futures after shutdown"}
    for attempt in range(3):
        l = c.post("/api/v1/workers/lease", json=hello(want=1), headers=TOKEN).json()["leases"][0]
        meta = {"lease": l["lease"], "worker": "w1", **err}
        out = c.post("/api/v1/workers/result", data={"meta": json.dumps(meta)}, headers=TOKEN).json()
        assert out["recorded"] == (attempt == 2)
    g = a.games[-1]
    assert g.error and g.meta["final_error"]
