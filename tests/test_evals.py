"""The evals service against a fake competition server: finds the top teams' finished battles
(not ours, not unfinished ones), stores games with seats and metrics, recomputes outdated metrics,
imports a crawl, and the website shows the report and a game."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arena import config, metrics, store
from arena.api.http import create_app
from arena.evals import collect, report
from arena.state import Arena

REPLAY = (Path(__file__).parent / "data" / "colosseum-seed1.replay").read_bytes()   # seat A wins
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def battle(bid, games, status="completed"):
    return {"match": {"id": bid, "teamAId": 10, "teamBId": 20, "status": status, "ranked": True,
                      "completedAt": NOW},
            "teamAName": "Alpha", "teamBName": "Beta", "teamAElo": 1900, "teamBElo": 1800,
            "mapName": "Colosseum", "games": games}


class Fake:
    def __init__(self):
        self.replays = 0
        self.battles = {
            100: battle(100, [{"id": 1001, "status": "completed", "winner": "b"},     # Beta sat in A
                              {"id": 1002, "status": "completed", "winner": "a"}]),
            101: battle(101, [{"id": 1011, "status": "running"}], status="running"),
            102: battle(102, [{"id": 1021, "status": "completed", "winner": "a"}]),     # vs us
        }

    def team(self):
        return {"id": 7}, False

    def leaderboard(self):
        return [{"id": 10, "rank": 1, "name": "Alpha", "elo": 1900}, {"id": 7, "rank": 2, "name": "us", "elo": 1850},
                {"id": 20, "rank": 3, "name": "Beta", "elo": 1800}], False

    def team_profile(self, tid):
        recent = {10: [{"id": 100, "opponentId": 20, "opponent": "Beta", "opponentElo": 1800, "at": NOW},
                       {"id": 101, "opponentId": 20, "opponent": "Beta", "opponentElo": 1800, "at": NOW},
                       {"id": 102, "opponentId": 7, "opponent": "us", "opponentElo": 1850, "at": NOW}],
                  20: [{"id": 100, "opponentId": 10, "opponent": "Alpha", "opponentElo": 1900, "at": NOW}]}
        return {"recent": recent.get(tid, [])}, False

    def battle(self, bid):
        return self.battles[bid], False

    def replay(self, gid):
        self.replays += 1
        return REPLAY

    def forget(self, prefix):
        pass


@pytest.fixture
def cfg(tmp_path):
    return config.Config(raw={"evals": {"procs": 0, "pace_seconds": 0}}, data_dir=tmp_path / "data")


def test_poll_stores_games_with_seats_and_metrics(cfg):
    fake = Fake()
    c = collect.Collector(cfg, client=fake, sleep=lambda s: None)
    c.poll()
    d = c.dir
    assert sorted(p.stem for p in (d / "battles").glob("*.json")) == ["100"]   # not running, not ours
    g = store.read_json(d / "games" / "1001.json")
    assert g["winner"] == "A" and g["seats"]["A"]["name"] == "Beta" and g["seats"]["B"]["name"] == "Alpha"
    g2 = store.read_json(d / "games" / "1002.json")
    assert g2["seats"]["A"]["name"] == "Alpha"
    assert store.read_json(d / "metrics" / "1001.json")["versions"] == metrics.versions()
    c.write_report()
    rep = store.read_json(d / "report.json")
    w = rep["windows"]["all"]
    assert w["games"] == 2 and w["battles"] == 1 and w["teams"] == 2
    assert w["endings"] == {"elimination": 2}
    assert any(x["n"] for x in w["eval"]) and w["metrics"]
    # the running battle is picked up once it finishes; stored ones aren't fetched again
    fake.battles[101] = battle(101, [{"id": 1011, "status": "completed", "winner": "a"}])
    c.poll()
    assert fake.replays == 3 and (d / "games" / "1011.json").exists()


def test_outdated_metrics_are_recomputed(cfg):
    c = collect.Collector(cfg, client=Fake(), sleep=lambda s: None)
    c.poll()
    p = c.dir / "metrics" / "1001.json"
    m = store.read_json(p)
    m["versions"]["eval"] = 0
    store.write_json(p, m)
    c2 = collect.Collector(cfg, client=Fake(), sleep=lambda s: None)
    c2.load()
    assert c2.stale == [1001]
    c2.recompute()
    assert store.read_json(p)["versions"] == metrics.versions() and not c2.stale


def test_import_and_retention(cfg, tmp_path):
    crawl = tmp_path / "crawl" / "M300"
    crawl.mkdir(parents=True)
    (crawl / "battle.json").write_text(json.dumps(
        {"listing": {"id": 300, "at": NOW}, "battle": battle(300, [{"id": 3001, "status": "completed", "winner": "a"}])}))
    (crawl / "M3001.replay").write_bytes(REPLAY)
    assert collect.import_dir(cfg, tmp_path / "crawl") == 1
    assert collect.import_dir(cfg, tmp_path / "crawl") == 0                      # idempotent
    rep = store.read_json(cfg.data_dir / "evals" / "report.json")
    assert rep["windows"]["all"]["games"] == 1
    c = collect.Collector(config.Config(raw={"evals": {"keep_gb": 0}}, data_dir=cfg.data_dir), client=False)
    c.retain()
    assert not list((c.dir / "replays").glob("*.replay"))


def test_auc():
    assert report.auc([1, 2, 3, 4], [False, False, True, True]) == 1.0
    assert report.auc([1, 1, 1, 1], [False, True, False, True]) == 0.5
    assert report.auc([1, 2], [True, True]) is None


def test_pages(cfg):
    collect.Collector(cfg, client=Fake(), sleep=lambda s: None).poll()
    c = collect.Collector(cfg, client=False)
    c.load()
    c.write_report()
    c.write_status()
    client = TestClient(create_app(Arena(cfg)))
    html = client.get("/evals").text
    assert "Does the eval pick the winner?" in html and "Beta" in html and "/evals/games/1001" in html
    assert "Does the eval" in client.get("/evals?w=5h").text
    v = client.get("/evals/games/1001").text
    assert "/api/v1/evals/games/1001/replay" in v and "Beta" in v
    assert client.get("/api/v1/evals/games/1001/replay").content[:4] == REPLAY[:4]
    assert client.get("/api/v1/evals?window=12h").json()["windows"].keys() == {"12h"}
    assert client.get("/api/v1/evals?window=bad").status_code == 400
    assert client.get("/api/v1/evals/status").json()["games"] == 2
    assert client.get("/evals/games/999").status_code == 404
