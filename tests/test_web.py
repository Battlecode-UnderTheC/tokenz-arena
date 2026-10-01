"""The website: every page renders from a fake arena (no judge games), escapes user text,
and the JSON routes that now share http.py's module-level helpers still answer."""

import pytest
from fastapi.testclient import TestClient

from arena import config, submit
from arena.api.http import create_app
from arena.state import Arena, Game
from tests.test_core import BOT, make_zip

EVIL = "<script>alert(1)</script>"


@pytest.fixture
def arena(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "one.map").write_text("MAP 4 4\nSYMMETRY xy\n")
    (maps / "two.map").write_text("MAP 4 4\n")
    cfg = config.Config(raw={"maps": {"official": str(maps)}}, data_dir=tmp_path / "data")
    a = Arena(cfg)

    def add(bot, lane, parent=None, note="", source=None):
        s, _ = submit.accept(a, blob=make_zip({**BOT, "x": bot + lane + str(parent)}), bot=bot,
                             owner="alice@github", lane=lane, parent=parent, note=note,
                             source=source, agent="worker-3")
        if lane == "ranked":
            submit.make_current(a, s)
            s.status = "rated"
        else:
            s.status = "done"
        s.opponents = [parent] if parent else []
        a.save_sub(s)
        return s

    base = add("base", "ranked", note="the base line",
               source="https://github.com/o/r/tree/0123abcd/bots/base")
    other = add("other", "ranked", note=f"other bot {EVIL}",
                source="https://github.com/o/r/tree/main/bots/other")
    draft = add("base", "draft", parent=base.id, note="lunge at enemy heads\nsecond line")
    rej, _ = submit.accept(a, blob=make_zip({**BOT, "y": "1"}), bot="other", owner="bob",
                           note="broken")
    rej.status, rej.reason, rej.check_log = "rejected", "did not build", "error: nope"
    a.save_sub(rej)
    a.ratings = {base.id: {"elo": 1100.0, "err": 40.0}, other.id: {"elo": 1000.0, "err": 40.0}}
    teams = {"A": {"deaths": {"hit a wall": 2}, "died": 2, "dragons_used": 4,
                   "points": {"p50": 5e6, "p99": 2e7, "max": 3e7}},
             "B": {"deaths": {"hit itself": 1}, "died": 1, "dragons_used": 3,
                   "points": {"p50": 5e6, "p99": 2e7, "max": 2e7}}}
    replays = cfg.replays
    replays.mkdir(parents=True)
    (replays / "1.replay").write_bytes(b"x")
    for i, (x, y, lane, w) in enumerate([(draft.id, base.id, "draft", "A"),
                                         (base.id, draft.id, "draft", "B"),
                                         (base.id, other.id, "placement", "A"),
                                         (other.id, base.id, "placement", None)]):
        a.add_game(Game(id=0, lane=lane, suite="official", map=["one", "two"][i % 2], seed=1,
                        a=x, b=y, winner=w, rounds=500 if w is None else 120,
                        reason="longest dragon, 9 to 4" if w else "draw",
                        stats={"teams": teams, "tiebreak": {"by": "longest", "winner": 9,
                                                            "loser": 4}},
                        replay="1.replay" if i == 0 else None))
    a.ids = {"base": base.id, "other": other.id, "draft": draft.id, "rej": rej.id}
    return a


@pytest.fixture
def client(arena):
    return TestClient(create_app(arena))


def get(client, url):
    r = client.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:500])
    return r.text


def test_leaderboard(client, arena):
    html = get(client, "/")
    assert "Leaderboard" in html and arena.ids["base"] in html and "1100" in html
    assert "Recent submissions" in html and "beats" in html          # draft beat its parent 2-0-0
    assert EVIL not in html and "&lt;script&gt;" in html
    assert "/api/v1/leaderboard" in html


def test_bot_page(client, arena):
    html = get(client, "/bots/base")
    assert arena.ids["draft"] in html and "Lineage" in html and "Head to head" in html
    assert "moving" not in html                                        # commit-sha source
    assert "moving" in get(client, "/bots/other")                       # /tree/main/
    assert client.get("/bots/nobody").status_code == 404


def test_submission_page(client, arena):
    d = arena.ids["draft"]
    html = get(client, f"/s/{d}")
    assert "Draft verdict" in html and "By map" in html and "Worst losses" in html
    assert "lunge at enemy heads\nsecond line" in html                  # note, preformatted
    assert f"/api/v1/submissions/{d}/source" in html
    assert 'id="promote"' in html and "/promote" in html               # finished draft
    assert 'id="promote"' not in get(client, f"/s/{arena.ids['base']}")
    html = get(client, f"/s/{arena.ids['rej']}")
    assert "did not build" in html and "error: nope" in html
    assert client.get("/s/nope-1").status_code == 404


def test_games(client, arena):
    html = get(client, "/games")
    assert "/games/1" in html and "watch" in html
    html = get(client, f"/games?sub={arena.ids['base']}&result=W")
    assert "/api/v1/games?sub=" in html
    viewer = get(client, "/games/1")
    assert "acquireVsCodeApi" in viewer and "/api/v1/games/1/replay" in viewer
    assert arena.ids["draft"] in viewer and "wins" in viewer           # header bar
    assert "no longer kept" in get(client, "/games/2")                  # replay pruned
    assert client.get("/games/99").status_code == 404


def test_maps(client):
    html = get(client, "/maps")
    assert "one" in html and "A wins %" in html and "/maps/official/one" in html
    assert 'data-view="map"' in get(client, "/maps/official/one")
    assert client.get("/maps/holdout/one").status_code == 404
    assert client.get("/maps/official/..%2Fsecret").status_code == 404


def test_ideas(client, arena):
    html = get(client, "/ideas?q=lunge")
    assert arena.ids["draft"] in html and "beat parent" in html
    assert "the base line" not in html
    assert "/api/v1/ideas?q=lunge" in html
    assert "did not build" in get(client, "/ideas?outcome=rejected")


def test_submit_queue_server(client):
    html = get(client, "/submit")
    assert "Submitting as <b>local</b>" in html and 'enctype="multipart/form-data"' in html
    html = get(client, "/submit")
    assert 'name="lane"' in html and 'name="agent"' in html
    assert "Playing now" in get(client, "/queue")
    assert "/api/v1/server" in get(client, "/server")


def test_json_routes_still_answer(client, arena):
    assert client.get("/api/v1/stats/maps").json()[0]["games"] >= 1
    assert set(client.get("/api/v1/queue").json()) == {"workers", "running", "checking", "waiting"}
    ideas = client.get("/api/v1/ideas", params={"outcome": "beat_parent"}).json()
    assert [i["id"] for i in ideas] == [arena.ids["draft"]]
    assert client.get("/api/v1/bots/base").json()["head_to_head"]
    assert client.get("/api/v1/bots/nobody").status_code == 404
    assert len(client.get("/api/v1/games", params={"map": "one"}).json()) == 2
    assert client.get("/api/v1/games/1").json()["watch_url"] == "/games/1"
    assert client.get("/api/v1/games/99").status_code == 404


def test_lineage_views(tmp_path):
    from arena import config, submit
    from arena.api.http import create_app
    from arena.state import Arena
    import io, zipfile
    from fastapi.testclient import TestClient

    def z(tag):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as f:
            f.writestr("bot.toml", '[project]\nlanguage = "py"\n')
            f.writestr("main.py", tag)
        return b.getvalue()
    a = Arena(config.Config(raw={"maps": {"worldgen": False}}, data_dir=tmp_path / "d"))
    root, _ = submit.accept(a, blob=z("r"), bot="base", owner="x")
    kid, _ = submit.accept(a, blob=z("k"), bot="base", owner="x", parent=root.id)
    c = TestClient(create_app(a))
    lines = c.get("/lineage").text                       # default: the Lines view
    assert "vchip" in lines and kid.id in lines and "base" in lines
    bots = c.get("/lineage?view=bots").text
    assert kid.id in bots and "ancestry" in bots
    graph = c.get("/lineage?view=graph").text
    assert "<svg" in graph and kid.id in graph and root.id in graph
    assert c.get("/lineage?view=tree").status_code == 200
