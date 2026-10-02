"""Map versions: games record their map's sha256 and only count while the map is unchanged;
older games are stamped once; judges with the same rules stay comparable."""

import os
import time

from arena import config, maps, state
from arena.migrate import stamp_map_shas
from arena.state import Arena, Game, current_game, is_rated


def make(tmp_path):
    off = tmp_path / "official"
    off.mkdir()
    (off / "one.map").write_text("MAP 4 4\n")
    srv = tmp_path / "data" / "maps" / "server"
    srv.mkdir(parents=True)
    (srv / "two.map").write_text("MAP 5 5\n")
    cfg = config.Config(raw={"maps": {"official": str(off)}}, data_dir=tmp_path / "data")
    maps._LIVE["at"] = 0.0
    return Arena(cfg), off, srv


def game(arena, suite, m, **meta):
    return arena.add_game(Game(id=0, lane="continuous", suite=suite, map=m, seed=1, a="x-1", b="y-1",
                               winner="A", rounds=100, meta={"engine": state.ENGINE, **meta}))


def test_changed_map_retires_its_games(tmp_path):
    arena, off, _ = make(tmp_path)
    g = game(arena, "official", "one", map_sha=maps.file_sha(off / "one.map"))
    assert is_rated(g)
    (off / "one.map").write_text("MAP 4 4\nTILE 0 0 1 1\n")      # re-published with real beds
    maps._LIVE["at"] = 0.0
    assert not is_rated(g) and not current_game(g)
    # a map that left play entirely (last week's rotating set) keeps its games
    h = game(arena, "rotating", "e1-gone", map_sha="abc")
    assert is_rated(h)


def test_a_map_that_moved_to_the_server_suite(tmp_path):
    arena, off, srv = make(tmp_path)
    (srv / "one.map").write_text("MAP 4 4\nSERVER COPY\n")           # supersedes official/one
    maps._LIVE["at"] = 0.0
    old = game(arena, "official", "one", map_sha="the-old-official-copy")
    same = game(arena, "official", "one", map_sha=maps.file_sha(srv / "one.map"))
    assert not is_rated(old) and is_rated(same)


def test_stamp_older_games(tmp_path):
    arena, off, srv = make(tmp_path)
    a = game(arena, "official", "one")
    b = game(arena, "server", "two")
    b.played = "2026-01-01T00:00:00Z"                              # before the file's last change
    c = game(arena, "server", "two")
    c.played = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
    d = game(arena, "official", "Colosseum")                      # a repo map: its known old hash
    for g in (a, b, c, d):
        g.meta.pop("map_sha", None)
    os.utime(srv / "two.map", (time.time(), time.time()))
    assert stamp_map_shas(arena) == 4
    assert a.meta["map_sha"] == maps.file_sha(off / "one.map")
    assert b.meta["map_sha"] == "older" and c.meta["map_sha"] == maps.file_sha(srv / "two.map")
    assert d.meta["map_sha"].startswith("c7549a41")
    assert stamp_map_shas(arena) == 0                              # idempotent
    assert Arena(arena.cfg).games[0].meta["map_sha"] == a.meta["map_sha"]   # saved


def test_same_rules_judges_stay_comparable(tmp_path, monkeypatch):
    arena, off, _ = make(tmp_path)
    g = game(arena, "official", "one", map_sha=maps.file_sha(off / "one.map"))
    g.meta["engine"] = "1.2.3"
    monkeypatch.setattr(state, "ENGINE", "1.2.7")
    assert current_game(g)
    monkeypatch.setattr(state, "ENGINE", "1.3.0")
    assert not current_game(g)
