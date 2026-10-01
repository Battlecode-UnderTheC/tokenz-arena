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
