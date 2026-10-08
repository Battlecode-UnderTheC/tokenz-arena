"""Games in SQLite (arena/gamedb.py): the one-time import, and the website catching up with the
engine's new and edited games from version stamps, without reading everything again."""

import json

from arena import config, store
from arena.gamedb import GameDB
from arena.state import Arena, Game


def cfg(tmp_path):
    return config.Config(raw={"maps": {"worldgen": False}}, data_dir=tmp_path / "d")


def game(i, a="p-1", b="q-1", **k):
    return Game(id=i, lane="continuous", suite="official", map="one", seed=1, a=a, b=b, winner="A",
                rounds=10, reason="elimination", **k)


def test_imports_the_json_games_once_and_leaves_the_files(tmp_path):
    c = cfg(tmp_path)
    for i in (1, 2, 5):
        store.write_json(c.games / f"{i}.json", {**game(i).__dict__})
    a = Arena(c)
    assert [g.id for g in a.games] == [1, 2, 5] and (c.games / "5.json").exists()
    a.add_game(game(0))
    assert a.games[-1].id == 6
    store.write_json(c.games / "9.json", {**game(9).__dict__})       # a stray file later: not imported again
    assert [g.id for g in Arena(c).games] == [1, 2, 5, 6]


def test_the_website_sees_new_and_edited_games(tmp_path):
    c = cfg(tmp_path)
    engine = Arena(c)
    engine.add_game(game(0))
    web = Arena(c, readonly=True)
    assert [g.id for g in web.games] == [1]
    g2 = engine.add_game(game(0))
    g1 = engine.games[0]
    g1.replay = None                                                # retention pruned its replay
    engine.save_game(g1)
    web.refresh()
    assert [g.id for g in web.games] == [1, g2.id] and web.games[0].replay is None
    kept = web.games[0]
    g1.a = "p-2"                                                    # a re-label
    engine.save_game(g1)
    web.refresh()
    assert web.games[0] is kept and kept.a == "p-2"                # the same object, edited in place
    assert [g.id for g in web.games_of("p-2")] == [1] and [g.id for g in web.games_of("p-1")] == [2]


def test_a_deletion_makes_the_website_load_everything(tmp_path):
    c = cfg(tmp_path)
    engine = Arena(c)
    for _ in range(3):
        engine.add_game(game(0))
    web = Arena(c, readonly=True)
    engine.gamedb.delete([2])
    web.refresh()
    assert [g.id for g in web.games] == [1, 3]


def test_since_returns_only_what_changed(tmp_path):
    db = GameDB(tmp_path / "g.db")
    db.put({"id": 1, "a": "x"})
    db.put({"id": 2, "a": "y"})
    v = db.version
    db.put({"id": 1, "a": "z"})
    rows, v2 = db.since(v)
    assert [r["a"] for r in rows] == ["z"] and v2 > v and db.since(v2) == ([], v2)
    assert json.loads(json.dumps(db.get(2))) == {"id": 2, "a": "y"}
