"""Duels and ephemeral bots (arena/duels.py): scheduled and played like any game, recorded apart."""

import json

import pytest
from fastapi.testclient import TestClient

from arena import config, duels, queue, submit, workers
from arena.api.http import create_app
from arena.daemon import Daemon
from arena.state import Arena

from tests.test_core import BOT, make_zip

TOKEN = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def setup(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    for m in ("one", "two"):
        (maps / f"{m}.map").write_text("MAP 4 4\nDRAGON 0 1 0 0\nDRAGON 1 1 3 3\n")
    cfg = config.Config(raw={"maps": {"official": str(maps), "tournament": str(tmp_path / "x"),
                                      "server": str(tmp_path / "y"), "worldgen": False},
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
    from arena.state import ENGINE
    return {"worker": "w1", "version": ENGINE, "slots": 64, **k}


def ephemeral(a, c, name="trial"):
    r = c.post("/api/v1/ephemeral", files={"file": ("b.zip", make_zip({**BOT, "v": name}))},
               data={"name": name}).json()
    assert r["status"] == "checking" and r["id"].startswith("eph-")
    book = duels.get(a)
    assert book.check_ephemeral(book.ephemeral[r["id"]])
    return r["id"]


def lease_all(a, c):
    out = []
    while True:
        got = c.post("/api/v1/workers/lease", json=hello(want=256), headers=TOKEN).json()["leases"]
        if not got:
            return out
        out += got


def finish(c, lease, winner="A"):
    meta = {"lease": lease, "worker": "w1", "winner": winner, "rounds": 50, "reason": "elimination",
            "seconds": 1.0, "stats": {}}
    return c.post("/api/v1/workers/result", data={"meta": json.dumps(meta)}, headers=TOKEN).json()


def test_ephemeral_bot_is_checked_kept_out_of_submissions_and_renewed(setup):
    a, d, c, subs = setup
    eid = ephemeral(a, c)
    assert eid not in a.subs
    assert all(x["id"] != eid for x in c.get("/api/v1/submissions").json())
    assert c.get(f"/api/v1/ephemeral/{eid}").json()["status"] == "ready"
    again = c.post("/api/v1/ephemeral", files={"file": ("b.zip", make_zip({**BOT, "v": "trial"}))}).json()
    assert again["id"] == eid and again["duplicate"]


def test_duel_plays_through_workers_and_never_becomes_an_arena_game(setup):
    a, d, c, (p, q) = setup
    eid = ephemeral(a, c)
    games_before, ratings_before = len(a.games), dict(a.ratings)
    r = c.post("/api/v1/duels", json={"a": p.id, "b": eid, "suites": ["official"], "seeds": "random:2"}).json()
    assert r["games_planned"] == 8                                   # 2 maps x 2 seeds x 2 seats
    did = r["id"]
    assert queue.get(a).view()["totals"]["by_lane"]["duel"] == 8     # backlog: the scaler rents for it
    leases = [x for x in lease_all(a, c) if x["game"]["suite"].startswith(did)]
    assert len(leases) == 8
    g0 = leases[0]["game"]
    suite = g0["suite"]
    assert c.get(f"/api/v1/workers/map/{suite}/{g0['map']['name']}", headers=TOKEN).status_code == 200
    assert c.get(f"/api/v1/workers/bot/{eid}", headers=TOKEN).status_code == 200
    seeds = {x["game"]["seed"] for x in leases}
    assert len(seeds) == 4 and not seeds & {1592614637}             # fresh seeds, not the ladder's
    for i, x in enumerate(leases):
        assert finish(c, x["lease"], "A" if i % 2 == 0 else "B")["recorded"]
    s = c.get(f"/api/v1/duels/{did}").json()
    assert s["status"] == "done" and s["games_played"] == 8 and s["games_left"] == 0
    rec = s["record"]
    assert rec["W"] + rec["D"] + rec["L"] == 8 and rec["ci"]
    assert set(s["by_suite"]) == {"official"} and s["by_starters"] == {"≤2": s["record"]}
    assert len(s["games"]) == 8
    assert len(a.games) == games_before and a.ratings == ratings_before    # nothing reached the ladder
    assert not any(k[0] == "duel" for k in queue.get(a).played)


def test_duel_games_come_after_drafts_and_placement(setup):
    a, d, c, (p, q) = setup
    c.post("/api/v1/duels", json={"a": p.id, "b": q.id, "suites": ["official"], "seeds": "random:1"})
    picked = queue.get(a).peek(100)
    lanes = [j.lane for j in picked]
    assert "duel" in lanes and "placement" in lanes
    assert lanes.index("duel") > max(i for i, x in enumerate(lanes) if x == "placement")


def test_a_released_duel_game_is_played_again(setup):
    a, d, c, (p, q) = setup
    did = c.post("/api/v1/duels", json={"a": p.id, "b": q.id, "suites": ["official"], "seeds": "random:1",
                                        "games": 1}).json()["id"]
    leases = [x for x in lease_all(a, c) if x["game"]["suite"].startswith(did)]
    assert len(leases) == 1
    reg = a.workers
    for l in reg.leases.values():
        l["expires"] = 0
    reg.reap()
    again = [x for x in lease_all(a, c) if x["game"]["suite"].startswith(did)]
    assert len(again) == 1 and finish(c, again[0]["lease"])["recorded"]
    assert c.get(f"/api/v1/duels/{did}").json()["status"] == "done"


def test_waiting_duel_is_not_backlog_until_its_bot_is_ready(setup):
    a, d, c, (p, q) = setup
    r = c.post("/api/v1/ephemeral", files={"file": ("b.zip", make_zip({**BOT, "v": "slow"}))}).json()
    did = c.post("/api/v1/duels", json={"a": p.id, "b": r["id"], "suites": ["official"]}).json()["id"]
    v = queue.get(a).view()
    assert v["waiting"][did]["status"] == "waiting" and "duel" not in v["totals"]["by_lane"]
    assert not [j for j in queue.get(a).peek(500) if j.duel]
    book = duels.get(a)
    book.check_ephemeral(book.ephemeral[r["id"]])
    assert [j for j in queue.get(a).peek(500) if j.duel]


def test_refusals_and_cancel(setup):
    a, d, c, (p, q) = setup
    assert c.post("/api/v1/duels", json={"a": p.id, "b": q.id, "suites": ["holdout"]}).status_code == 400
    assert c.post("/api/v1/duels", json={"a": p.id, "b": "nobody"}).status_code == 400
    assert c.post("/api/v1/duels", json={"a": p.id, "b": p.id}).status_code == 400
    did = c.post("/api/v1/duels", json={"a": p.id, "b": q.id, "suites": ["official"]}).json()["id"]
    assert c.delete(f"/api/v1/duels/{did}").json()["status"] == "cancelled"
    assert not [j for j in queue.get(a).peek(500) if j.duel]


def test_duels_survive_a_restart(setup):
    a, d, c, (p, q) = setup
    did = c.post("/api/v1/duels", json={"a": p.id, "b": q.id, "suites": ["official"], "seeds": "random:1"}).json()["id"]
    leases = [x for x in lease_all(a, c) if x["game"]["suite"].startswith(did)]
    finish(c, leases[0]["lease"])
    b2 = duels.Book(a)
    assert b2.left(did) == 3 and len(b2.pending[did]) == 3


def test_duels_pages(setup):
    a, d, c, (p, q) = setup
    did = c.post("/api/v1/duels", json={"a": p.id, "b": q.id, "suites": ["official"], "seeds": "random:1"}).json()["id"]
    r = c.get("/duels")
    assert r.status_code == 200 and "Duels" in r.text and 'href="/duels"' in r.text
    r = c.get(f"/duels/{did}")
    assert r.status_code == 200 and f'const did = "{did}";' in r.text
    assert "fetch(`/api/v1/duels/${did}`)" in r.text and "$" + did not in r.text   # the script asks for this duel
    assert c.get("/duels/duel-x<script>").status_code == 404
