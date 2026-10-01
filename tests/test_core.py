"""Scheduler, rating and submission checks on a fake arena (no judge games)."""

import io
import os
import zipfile

import pytest

from arena import config, rating, scheduler, submit
from arena.state import Arena, Game


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


BOT = {"bot.toml": '[project]\nlanguage = "py"\n', "main.py": "print('hi')\n"}


@pytest.fixture
def arena(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    for m in ("one", "two"):
        (maps / f"{m}.map").write_text("MAP 4 4\n")
    cfg = config.Config(raw={"maps": {"official": str(maps), "tournament": str(tmp_path / "none"),
                                      "worldgen": False}}, data_dir=tmp_path / "data")
    return Arena(cfg)


def add(arena, bot, lane="ranked", files=BOT, parent=None):
    s, _ = submit.accept(arena, blob=make_zip({**files, "x": bot + lane + str(parent)}), bot=bot,
                         owner="alice", lane=lane, parent=parent)
    (arena.cfg.bots / s.id).mkdir(parents=True)
    if lane == "ranked":
        submit.make_current(arena, s)
    else:
        s.status = "queued"
    return s


def test_rejects_bad_zips(arena):
    with pytest.raises(submit.Rejected):
        submit.accept(arena, blob=b"nope", bot="x", owner="a")
    with pytest.raises(submit.Rejected):
        submit.accept(arena, blob=make_zip({"main.py": ""}), bot="x", owner="a")
    with pytest.raises(submit.Rejected):
        submit.accept(arena, blob=make_zip({"../evil": "", "bot.toml": ""}), bot="x", owner="a")


def test_duplicate_returns_original(arena):
    blob = make_zip(BOT)                  # one zip: zips stamp the time, so two builds can differ
    a, new = submit.accept(arena, blob=blob, bot="x", owner="a")
    b, again = submit.accept(arena, blob=blob, bot="y", owner="b")
    assert new and not again and a.id == b.id


def test_draft_plays_parent_and_top_both_seats(arena):
    p = add(arena, "base")
    q = add(arena, "other")
    d = add(arena, "base", lane="draft", parent=p.id)
    jobs = scheduler.owed_for(arena, d)
    assert {(j.a, j.b) for j in jobs} == {(d.id, p.id), (p.id, d.id), (d.id, q.id), (q.id, d.id)}
    assert len(jobs) == 2 * 2 * 2   # 2 opponents x 2 maps x 2 seats


def test_settle_and_rating(arena):
    p = add(arena, "base")
    q = add(arena, "other")
    for j in scheduler.owed_for(arena, q):
        arena.add_game(Game(id=0, lane=j.lane, suite=j.suite, map=j.key[3], seed=j.seed, a=j.a,
                            b=j.b, winner="A" if j.a == q.id else "B", rounds=500))
    scheduler.settle(arena)
    assert q.status == "rated"
    r = rating.fit(arena)
    assert r[q.id]["elo"] > r[p.id]["elo"]


def test_identity_from_tailscale_headers():
    from arena import who
    h = {"tailscale-user-login": "alice@github", "authorization": "Bearer worker-3"}
    assert who.identify(h, "127.0.0.1").owner == "alice@github"
    who.TRUST_HEADERS = False
    assert who.identify(h, "100.1.2.3").owner != "alice@github"   # a forged header from outside
    who.TRUST_HEADERS = True
    try:
        c = who.identify(h, "100.1.2.3")
        assert (c.owner, c.agent) == ("alice@github", "worker-3")
    finally:
        who.TRUST_HEADERS = False


def test_placement_plays_server_maps_on_their_own_seeds(arena):
    from arena import store
    server = arena.cfg.data_dir / "maps" / "server"
    server.mkdir(parents=True)
    (server / "dilemma.map").write_text("MAP 4 4\n")
    store.write_json(arena.cfg.data_dir / "server" / "seeds.json", {"dilemma": [11, 22, 33]})
    p = add(arena, "base")
    q = add(arena, "other")
    jobs = [j for j in scheduler.owed_for(arena, q) if j.suite == "server"]
    assert sorted({j.seed for j in jobs}) == [11, 22]          # suite_seeds defaults to 2
    assert len(jobs) == 2 * 2                                  # 2 seeds x 2 seats vs base


def test_suites_from_config_and_holdout_hidden(arena):
    from arena.api import http
    tour = arena.cfg.data_dir / "tour"
    tour.mkdir(parents=True)
    (tour / "maze.map").write_text("MAP 4 4\n")
    arena.cfg.raw["maps"]["tournament"] = str(tour)
    p = add(arena, "base")
    d = add(arena, "base", lane="draft", parent=p.id)
    suites = {j.suite for j in scheduler.owed_for(arena, d)}
    assert suites == {"official", "tournament"}
    arena.cfg.raw.setdefault("play", {})["draft_suites"] = ["official"]
    d2 = add(arena, "other", lane="draft", parent=p.id)
    assert {j.suite for j in scheduler.owed_for(arena, d2)} == {"official"}
    g = arena.add_game(Game(id=0, lane="holdout", suite="holdout", map="h123", seed=1, a=p.id,
                            b=d.id, winner="A", rounds=500))
    assert http.find_game(arena, g.id) is None
    assert all(x.suite != "holdout" for x in http.find_games(arena, limit=100))


def test_rotating_epochs():
    from arena import maps
    assert maps.epoch(maps.EPOCH_START + 6 * 86400) == 0
    assert maps.epoch(maps.EPOCH_START + 7 * 86400) == 1


def test_interval_flags_noise():
    from arena.report import interval
    assert interval({"W": 9, "D": 0, "L": 6})["significant"] is False      # 60% of 15: noise
    big = interval({"W": 120, "D": 0, "L": 80})                             # 60% of 200
    assert big["significant"] is True and big["ci"][0] > 50


def test_old_engine_games_are_historical(arena):
    from arena import state
    p = add(arena, "base")
    q = add(arena, "other")
    seed = arena.cfg.seeds("official")[0]
    old = Game(id=0, lane="placement", suite="official", map="one", seed=seed, a=p.id, b=q.id,
               winner="A", rounds=500, meta={"engine": "1.0.0"})
    new = Game(id=0, lane="placement", suite="official", map="one", seed=seed, a=p.id, b=q.id,
               winner="A", rounds=500, meta={"engine": state.ENGINE})
    assert not state.is_rated(old) and state.is_rated(new)
    arena.add_game(old)                        # an old-rules game doesn't satisfy the job
    key = (p.id, q.id, "official", "one", seed)
    assert key in {j.key for j in scheduler.next_jobs(arena, 100)}


def test_map_name_lint(arena, tmp_path):
    root = tmp_path / "bot"
    root.mkdir()
    (root / "main.py").write_text('if name == "Colosseum":\n    go()\nx = "hello world"\n')
    real = config.Config(raw={}, data_dir=tmp_path / "d")
    warnings = submit._map_names(real, root)
    assert len(warnings) == 1 and "Colosseum" in warnings[0]


def test_server_maps_supersede_official_copies(arena):
    server = arena.cfg.data_dir / "maps" / "server"
    server.mkdir(parents=True)
    (server / "uno.map").write_text("MAP 4 4\nMAP_NAME One\n")       # same map as official one.map
    assert [p.stem for p in scheduler.maps(arena, "official")] == ["two"]
    assert [p.stem for p in scheduler.maps(arena, "server")] == ["uno"]


def test_note_edit_and_lineage(arena):
    from fastapi.testclient import TestClient
    from arena.api.http import create_app
    p = add(arena, "base")
    d = add(arena, "base", lane="draft", parent=p.id)
    c = TestClient(create_app(arena))
    alice = {"tailscale-user-login": "alice"}            # add() submits as alice
    assert c.patch(f"/api/v1/submissions/{d.id}", json={"note": "new"},
                   headers={"tailscale-user-login": "bob"}).status_code == 403
    r = c.patch(f"/api/v1/submissions/{d.id}", json={"note": "new"}, headers=alice)
    assert r.status_code == 200 and r.json()["note"] == "new" and len(r.json()["note_history"]) == 1
    forest = c.get("/api/v1/lineage").json()
    assert forest[0]["id"] == p.id and forest[0]["children"][0]["id"] == d.id
    assert c.get(f"/api/v1/lineage?root={d.id}").json()[0]["id"] == d.id
    assert c.get("/lineage").status_code == 200 and d.id in c.get("/lineage").text
    assert c.get(f"/s/{d.id}", headers=alice).status_code == 200


def test_set_parent_reschedules_and_refuses_cycles(arena):
    from fastapi.testclient import TestClient
    from arena.api.http import create_app
    p = add(arena, "base")
    q = add(arena, "other")
    d = add(arena, "base", lane="draft", parent=p.id)
    scheduler.owed_for(arena, d)                       # freezes its opponents
    d.status = "done"
    c = TestClient(create_app(arena))
    alice = {"tailscale-user-login": "alice"}
    r = c.patch(f"/api/v1/submissions/{p.id}", json={"parent": d.id}, headers=alice)
    assert r.status_code == 400 and "cycle" in r.json()["detail"]
    arena.subs[d.id].opponents = [p.id]                # as if q weren't among its opponents
    r = c.patch(f"/api/v1/submissions/{d.id}", json={"parent": q.id}, headers=alice)
    assert r.status_code == 200, r.text
    d2 = r.json()
    assert d2["parent"] == q.id and d2["status"] == "queued" and d2["parent_history"][0]["parent"] == p.id
    assert any(q.id in (j.a, j.b) for j in scheduler.owed_for(arena, arena.subs[d.id]))
    assert c.patch(f"/api/v1/submissions/{d.id}", json={"parent": None},
                   headers={"tailscale-user-login": "bob"}).status_code == 403


def test_quick_placement_then_low_priority_topup(arena):
    bots = [add(arena, f"b{i}") for i in range(8)]          # each placed, now rated in turn
    for b in bots[:-1]:
        b.status = "rated"
    new = add(arena, "b0", parent=bots[0].id)               # replaces b0, placing
    jobs = scheduler.owed_for(arena, new)
    opps = {j.a if j.b == new.id else j.b for j in jobs}
    assert bots[0].id in opps and len(opps) <= 1 + 5        # parent + top 5
    assert {j.priority for j in jobs} == {scheduler.PLACEMENT}
    new.status = "rated"
    later = scheduler.owed_for(arena, new)
    assert {j.priority for j in later} == {scheduler.CONTINUOUS}
    assert len({j.a if j.b == new.id else j.b for j in later}) > len(opps)


def test_verdict_splits_by_how_games_ended(arena):
    from arena import report
    p = add(arena, "base")
    d = add(arena, "base", lane="draft", parent=p.id)
    scheduler.owed_for(arena, d)
    seed = arena.cfg.seeds("official")[0]
    for reason, winner in (("elimination", "A"), ("longer queen, 3 to 1", "B"), ("longest dragon, 9 to 4", "A")):
        arena.add_game(Game(id=0, lane="draft", suite="official", map="one", seed=seed, a=d.id,
                            b=p.id, winner=winner, rounds=500, reason=reason))
    vp = report.verdict(arena, d.id)["vs_parent"]
    assert vp["by_end"]["longer queen"]["L"] == 1 and vp["by_end"]["elimination"]["W"] == 1
    assert vp["server_maps"] is None


def test_retire_and_promote_replaces(arena):
    from fastapi.testclient import TestClient
    from arena.api.http import create_app
    a1 = add(arena, "queen-a")
    b1 = add(arena, "queen-b")
    d = add(arena, "queen-c", lane="draft")
    d.status = "done"
    c = TestClient(create_app(arena))
    alice, bob = {"tailscale-user-login": "alice"}, {"tailscale-user-login": "bob"}
    assert c.post(f"/api/v1/submissions/{a1.id}/retire", headers=bob).status_code == 403
    r = c.post(f"/api/v1/submissions/{a1.id}/retire", headers=alice)
    assert r.status_code == 200 and r.json()["status"] == "retired"
    assert arena.bots["queen-a"]["current"] is None and a1.id not in {s.id for s in arena.current()}
    r = c.post(f"/api/v1/submissions/{d.id}/promote?replaces={b1.id}", headers=alice)
    assert r.status_code == 200 and r.json()["status"] == "placing"
    assert arena.subs[b1.id].status == "retired" and arena.subs[b1.id].retired_by == "alice"


def test_migrate_renames_with_aliases(arena, tmp_path):
    from arena import migrate
    from arena.state import Arena as A
    q1, q2, q3 = add(arena, "gst-queen-1"), add(arena, "gst-queen-2"), add(arena, "gst-queen-3")
    child = add(arena, "gst-queen-5", lane="draft", parent=q3.id)
    arena.add_game(Game(id=0, lane="placement", suite="official", map="one", seed=1, a=q1.id,
                        b=q3.id, winner="A", rounds=500))
    tsv = tmp_path / "r.tsv"
    tsv.write_text("old_id\tnew_bot\tnew_id\tteam\tlane\n" + "".join(
        f"{s.id}\tqueen\tqueen-{i}\tsearch\tx\n" for i, s in ((1, q1), (2, q2), (3, q3), (5, child))))
    dry = migrate.rename_submissions(arena.cfg, tsv)
    assert dry["dry_run"] and dry["renamed"] == 4 and (arena.cfg.submissions / q1.id).exists()
    rep = migrate.rename_submissions(arena.cfg, tsv, dry_run=False)
    assert rep["lines"] == {"queen": "queen-3"} and len(rep["retired"]) == 2
    b = A(arena.cfg)
    assert {"queen-1", "queen-2", "queen-3", "queen-5"} <= set(b.subs)
    assert b.subs["queen-5"].parent == "queen-3" and b.subs["gst-queen-3-1"].id == "queen-3"
    assert b.bots["queen"]["current"] == "queen-3" and "gst-queen-1" not in b.bots
    assert b.subs["queen-1"].status == "retired" and b.games[0].a == "queen-1"
    assert b.resolve("gst-queen-5-1") == "queen-5"


def test_migrate_reuses_a_freed_id(arena, tmp_path):
    from arena import migrate
    from arena.state import Arena as A
    old = add(arena, "gst-queen-1")                           # gst-queen-1-1 -> queen-1
    newer = add(arena, "queen", lane="draft", parent=old.id)  # queen-1 (really v6) -> queen-6
    assert newer.id == "queen-1"
    tsv = tmp_path / "r.tsv"
    tsv.write_text("old_id\tnew_bot\tnew_id\tteam\tlane\n"
                   f"{old.id}\tqueen\tqueen-1\tsearch\tranked\nqueen-1\tqueen\tqueen-6\tsearch\tdraft\n")
    migrate.rename_submissions(arena.cfg, tsv, dry_run=False)
    b = A(arena.cfg)
    assert b.subs["queen-6"].parent == "queen-1" and b.subs["queen-1"].sha256 == old.sha256
    assert b.resolve("gst-queen-1-1") == "queen-1" and b.resolve("queen-1") == "queen-1"
    assert (arena.cfg.bots / "queen-6").exists() and (arena.cfg.bots / "queen-1").exists()


def test_migrate_removes_then_renames(arena, tmp_path):
    from arena import migrate
    from arena.state import Arena as A
    old = add(arena, "gst-queen-1")
    clash = add(arena, "queen", lane="draft", parent=old.id)          # queen-1, to be removed
    arena.add_game(Game(id=0, lane="draft", suite="official", map="one", seed=1, a=clash.id,
                        b=old.id, winner="A", rounds=500))
    tsv = tmp_path / "r.tsv"
    tsv.write_text(f"old_id\tnew_bot\tnew_id\tteam\tlane\n{old.id}\tqueen\tqueen-1\tsearch\tranked\n")
    rep = migrate.rename_submissions(arena.cfg, tsv, dry_run=False, remove=["queen-1"])
    assert rep["removed"]["games"] == 1
    b = A(arena.cfg)
    assert b.subs["queen-1"].sha256 == old.sha256 and not b.games
    assert (arena.cfg.data_dir / "removed" / "submissions" / "queen-1").exists()


def test_replays_compressed_and_pruned(arena):
    from arena import retention
    arena.cfg.replays.mkdir(parents=True)
    games = []
    for i, lane in enumerate(["draft", "server", "draft", "draft"]):
        g = arena.add_game(Game(id=0, lane=lane, suite="official", map="one", seed=i, a="x", b="y",
                                winner="A", rounds=10))
        (arena.cfg.replays / f"{g.id}.replay").write_bytes(os.urandom(200_000))   # incompressible
        g.replay = f"{g.id}.replay"
        games.append(g)
    assert retention.compress_existing(arena) == 4
    assert all(retention.is_gzip(arena.cfg.replays / g.replay) for g in games)
    arena.cfg.raw["retention"] = {"replay_cap_gb": 5e-4}               # ~540 KB of 800 KB: prunes
    retention.prune(arena)
    assert games[1].replay is not None                                  # server replay kept
    assert games[0].replay is None and not (arena.cfg.replays / "1.replay").exists()
