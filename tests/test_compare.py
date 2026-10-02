"""The comparison of two bots: per map, round by round, queens, phases; the page and its JSON."""

from pathlib import Path

from fastapi.testclient import TestClient

from arena import metrics, store
from arena.api.http import create_app
from arena.state import Game
from tests.test_core import add, arena  # noqa: F401  (fixture)

REPLAY = (Path(__file__).parent / "data" / "colosseum-seed1.replay").read_bytes()   # A wins; B's queen hits a wall


def test_compare(arena):
    p, q = add(arena, "base"), add(arena, "other")
    m = metrics.compute(REPLAY)
    assert m["queens"]["B"] == {"round": 68, "cause": "hit a wall", "length": 18} and m["queens"]["A"] is None
    for i, (a, b) in enumerate([(p.id, q.id), (q.id, p.id), (p.id, q.id)]):
        g = arena.add_game(Game(id=0, lane="continuous", suite="official", map=["one", "two", "one"][i], seed=1,
                                a=a, b=b, winner="A", rounds=68, reason="elimination"))
        store.write_json(arena.cfg.data_dir / "metrics" / f"{g.id}.json", m)
    from arena.metrics.compare import compare
    d = compare(arena, p.id, q.id)
    sp, sq = d["sides"][p.id], d["sides"][q.id]
    assert sp["games"] == 3 and sp["record"] == {"W": 2, "D": 0, "L": 1}     # seat A always wins
    assert sp["queen"]["died"] == 1 and sp["queen"]["causes"] == {"hit a wall": 1}   # when it sat in B
    assert sq["queen"]["died"] == 2 and sq["queen"]["median_death_round"] == 68
    assert sp["queen"]["phases"]["early"] == 1 and len(sp["curves"]["dragons"]["mine"]) == 50
    assert {r["map"] for r in d["per_map"]} == {"one", "two"}
    h2h_one = compare(arena, p.id, q.id, map="one", scope="h2h")
    assert h2h_one["sides"][p.id]["games"] == 2
    c = TestClient(create_app(arena))
    page = c.get(f"/compare?a={p.id}&b={q.id}").text
    assert "Queens" in page and "hit a wall" in page and "Early, mid, late" in page and "<svg" in page
    assert c.get(f"/api/v1/compare?a={p.id}&b={q.id}&scope=h2h").json()["scope"] == "h2h"
    assert c.get(f"/api/v1/compare?a={p.id}&b=nope-1").status_code == 404
    assert "Compare" in c.get("/compare").text
