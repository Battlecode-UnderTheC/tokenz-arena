"""Scheduler, rating and submission checks on a fake arena (no judge games)."""

import io
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
    a, new = submit.accept(arena, blob=make_zip(BOT), bot="x", owner="a")
    b, again = submit.accept(arena, blob=make_zip(BOT), bot="y", owner="b")
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
