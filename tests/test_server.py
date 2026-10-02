"""The server bridge against a fake competition server (a local HTTP stub): the key, the
cache and rate limits, the signed replay redirect, battle download, the champion gate with
auto-submit, challenges with the quota check, and the /api/v1/server endpoints. Nothing here
talks to the real server."""

import gzip
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from arena import config, rating, submit
from arena.server import bridge as bridge_mod
from arena.server import replay
from arena.server.bridge import Bridge, Refused
from arena.server.client import Client, NoKey, RateLimited, load_key
from arena.state import Arena, Game

KEY = "bc_TESTKEY_not_a_real_one_1234567890"
REPLAY = (Path(__file__).parent / "data" / "colosseum-seed1.replay").read_bytes()
TEAM = 328


# ---------------------------------------------------------------------------- the fake server
class Fake:
    def __init__(self):
        self.requests = []            # (method, path, has_auth, body)
        self.routes = {}              # (method, path) -> (status, body, headers)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def set(self, method, path, body, status=200, headers=None):
        self.routes[(method, path)] = (status, body, headers or {})

    def calls(self, method=None, prefix=""):
        return [r for r in self.requests if (not method or r[0] == method) and r[1].startswith(prefix)]

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append((method, self.path, "Authorization" in self.headers, body,
                                      self.headers.get("Authorization")))
                hit = fake.routes.get((method, self.path))
                if hit is None and self.path.startswith("/signed/"):
                    hit = (200, gzip.compress(REPLAY), {"Content-Type": "application/octet-stream"})
                if hit is None:
                    hit = (404, {"error": "not found"}, {})
                status, out, headers = hit
                raw = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

        return H


class FakeWeb:
    def __init__(self, remaining=60):
        self.remaining = remaining
        self.last_error = None

    def availability(self, team_id):
        return {"dev": False, "remaining": self.remaining, "freeIn": 12}

    def bracket(self, tid, done, names=()):
        return None


def battle(gid, map_name, winner, seed, status="completed", log=None, games=None):
    return {"match": {"id": gid, "teamAId": TEAM, "teamBId": 852, "mapId": 13, "ranked": True,
                      "status": status, "winner": winner, "scoresA": 15, "scoresB": 0,
                      "seriesId": "s-1", "eloChangeA": 5, "eloChangeB": -5,
                      "requestedAt": "2026-10-01T11:59:38.566Z", "startedAt": "2026-10-01T11:59:39.981Z",
                      "completedAt": "2026-10-01T11:59:49.298Z", "log": log, "seed": seed},
            "mapName": map_name, "teamAName": "UnderTheC", "teamBName": "C--", "teamAElo": 1548,
            "teamBElo": 1842, "games": games or [], "wait": None}


@pytest.fixture
def fake():
    f = Fake()
    f.set("GET", "/api/v1/me", {"user": {"id": "u1", "username": "syeyoung"},
                                "team": {"id": TEAM, "name": "UnderTheC", "elo": 1548},
                                "key": {"prefix": KEY[:12]}})
    f.set("GET", "/api/v1/team", {"team": {"id": TEAM, "name": "UnderTheC", "elo": 1548, "code": 999,
                                           "members": [{"id": "u1", "username": "syeyoung"}]},
                                  "record": {"wins": 9, "draws": 0, "losses": 7}, "rank": 45, "peak": 1737,
                                  "requests": [{"secret": "x"}]})
    f.set("GET", "/api/v1/submissions", [
        {"id": 500, "name": "live-bot", "status": "active", "language": "cpp", "version": 2,
         "uploadedAt": "2026-10-01T10:00:00.000Z", "uploadedBy": "u1", "wins": 3, "draws": 0,
         "losses": 2, "byMap": {"Devil": {"wins": 1, "draws": 0, "losses": 0}}, "sourceHash": "nope",
         "buildLog": "long", "storageKey": "secret/path.zip"},
        {"id": 400, "name": "old-bot", "status": "idle", "language": "cpp", "version": 1,
         "uploadedAt": "2026-09-30T10:00:00.000Z", "uploadedBy": "u1", "sourceHash": "nope2"}])
    f.set("GET", "/api/v1/maps", [{"id": 13, "name": "Devil", "sizeLabel": "16×16", "active": True,
                                   "mapText": "MAP 16 16\nMAP_NAME Devil\n"},
                                  {"id": 17, "name": "Prisoners Dilemma", "active": True,
                                   "mapText": "MAP 32 16\nMAP_NAME Prisoners Dilemma\n"}])
    f.set("GET", "/api/v1/battles?limit=20", [
        {"id": 900, "ranked": True, "opponentId": 852, "opponent": "C--", "opponentElo": 1842,
         "eloChange": 5, "results": ["win", "loss"], "wins": 1, "losses": 1, "outcome": "draw",
         "replayId": 900, "at": "2026-10-01T12:00:10.268Z"},
        {"id": 950, "ranked": False, "opponentId": 7, "opponent": "Slowpoke", "outcome": None,
         "results": [], "at": "2026-10-01T12:05:00.000Z"}])
    games = [{"id": 900, "status": "completed", "winner": "a", "hasReplay": True, "mapName": "Devil"},
             {"id": 901, "status": "failed", "winner": "b", "hasReplay": False, "mapName": "Prisoners Dilemma"}]
    f.set("GET", "/api/v1/battles/900", battle(900, "Devil", "a", "00000000000000ff", games=games))
    f.set("GET", "/api/v1/battles/901", battle(901, "Prisoners Dilemma", "b", "0000000000000010",
                                               status="failed", log="team A timed out on round 3", games=games))
    f.set("GET", "/api/v1/battles/950", battle(950, "Devil", None, None, status="queued",
                                               games=[{"id": 950, "status": "queued", "mapName": "Devil"}]))
    f.set("GET", "/api/v1/battles/900/replay", b"", status=302,
          headers={"Location": f"{f.url}/signed/900?sig=abc"})
    f.set("GET", "/api/v1/ratings", {"ladder": [{"rank": 1, "id": 20, "name": "calc", "elo": 2012},
                                                {"rank": 11, "id": 852, "name": "C--", "elo": 1842},
                                                {"rank": 12, "id": 853, "name": "C++ fans", "elo": 1800},
                                                {"rank": 45, "id": TEAM, "name": "UnderTheC", "elo": 1548}],
                                     "histories": {str(TEAM): [{"date": "2026-09-21", "elo": 1500}]}})
    f.set("GET", "/api/v1/leaderboard", [{"rank": 1, "id": 20, "name": "calc", "elo": 2012, "members": [{}],
                                          "history": [{}]},
                                         {"rank": 45, "id": TEAM, "name": "UnderTheC", "elo": 1548}])
    f.set("GET", "/api/v1/teams/852", {"team": {"id": 852, "name": "C--", "elo": 1842, "code": 1,
                                                "members": [{"id": "m", "username": "GG"}]},
                                       "record": {"wins": 1, "draws": 0, "losses": 2}, "rank": 11,
                                       "recent": [], "history": []})
    yield f
    f.server.shutdown()


def make_zip(tag: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bot.toml", '[project]\nlanguage = "cpp"\n')
        z.writestr("main.cpp", f"// {tag}\nint main(){{}}\n")
    return buf.getvalue()


@pytest.fixture
def arena(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "one.map").write_text("MAP 4 4\n")
    cfg = config.Config(raw={"maps": {"official": str(maps), "server": str(tmp_path / "server-maps")},
                             "server": {"auto_submit": True, "min_hours": 4}},
                        data_dir=tmp_path / "data")
    return Arena(cfg)


@pytest.fixture
def bridge(arena, fake):
    b = Bridge(arena, client=Client(KEY, url=fake.url), web=FakeWeb())
    arena.server_bridge = b
    return b


def add(arena, bot, note=""):
    s, _ = submit.accept(arena, blob=make_zip(bot), bot=bot, owner="alice", lane="ranked", note=note)
    (arena.cfg.bots / s.id).mkdir(parents=True)
    s.language = "cpp"
    submit.make_current(arena, s)
    return s


# ---------------------------------------------------------------------------- the key
def test_load_key_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))          # so the toolkit fallback finds nothing
    bare = tmp_path / "k"
    bare.write_text(KEY + "\n")
    assert load_key(str(bare)) == KEY
    toolkit = tmp_path / "keys.json"
    toolkit.write_text(json.dumps({"https://game.battlecode.au": KEY}))
    assert load_key(str(toolkit)) == KEY
    assert load_key(str(tmp_path / "missing")) is None
    bad = tmp_path / "bad"
    bad.write_text("not a key")
    assert load_key(str(bad)) is None


def test_no_key_never_crashes(arena):
    b = Bridge(arena, client=Client(None))
    assert b.summary()["status"] == "no key configured"
    assert b.download() == 0
    with pytest.raises(NoKey):
        b.client.leaderboard()


# ---------------------------------------------------------------------------- the client
def test_cache_and_rate_limit(fake):
    t = [1000.0]
    c = Client(KEY, url=fake.url, clock=lambda: t[0])
    c.leaderboard()
    c.leaderboard()
    assert len(fake.calls("GET", "/api/v1/leaderboard")) == 1          # cached for 60 s
    t[0] += 61
    fake.set("GET", "/api/v1/leaderboard", {"error": "slow down"}, status=429, headers={"Retry-After": "20"})
    data, stale = c.leaderboard()
    assert stale and data[0]["name"] == "calc"                          # the cached copy, marked stale
    with pytest.raises(RateLimited) as e:
        c.ratings()                                                     # same bucket: blocked, no cache
    assert 0 < e.value.retry_after <= 20
    assert len(fake.calls("GET", "/api/v1/ratings")) == 0
    assert KEY not in repr(c) and KEY not in str(e.value)


def test_replay_redirect_drops_the_key(fake):
    c = Client(KEY, url=fake.url)
    blob = c.replay(900)
    assert replay.gunzip(blob) == REPLAY
    api = fake.calls("GET", "/api/v1/battles/900/replay")
    signed = fake.calls("GET", "/signed/")
    assert api and api[0][2] is True                 # the API call carries the key
    assert signed and signed[0][2] is False          # the signed link does not


def test_finished_battles_cached_forever(fake):
    t = [0.0]
    c = Client(KEY, url=fake.url, clock=lambda: t[0])
    c.battle(900)
    c.battle(950)
    t[0] += 10_000
    c.battle(900)
    c.battle(950)
    assert len(fake.calls("GET", "/api/v1/battles/900")) == 1
    assert len(fake.calls("GET", "/api/v1/battles/950")) == 2


# ---------------------------------------------------------------------------- replays
def test_replay_summary_matches_the_judge():
    d = replay.load(REPLAY)
    s = replay.summarize(d)
    # `unswbc run Colosseum --sandbox --seed 1`, two template bots: "team A wins after 68 rounds
    # (by elimination)", "round 67: bot 1 (team B) died: hit a wall"
    assert (s["winner"], s["rounds"], s["reason"]) == ("A", 68, "elimination")
    assert d["seed"] == 1 and (d["botA"], d["botB"]) == ("a", "b")
    assert s["stats"]["teams"]["B"]["deaths"] == {"hit a wall": 1}
    assert replay.load(gzip.compress(REPLAY))["result"] == d["result"]


def test_verdict_wording():
    st = lambda q, l, t: {"queen": q, "longest": l, "total": t}
    assert replay.verdict({"end": "roundLimit", "winner": "B", "A": st(3, 9, 20), "B": st(5, 4, 10)}) \
        == "longer queen, 5 to 3"
    assert replay.verdict({"end": "roundLimit", "winner": "A", "A": st(5, 9, 20), "B": st(5, 9, 10)}) \
        == "total length, 20 to 10"


# ---------------------------------------------------------------------------- download
def test_download_imports_battles(bridge, arena, fake):
    live = add(arena, "live")
    bridge.versions["500"] = {"arena": live.id}
    n = bridge.download()
    assert n == 2
    games = [g for g in arena.games if g.lane == "server"]
    assert len(games) == 2
    g1 = next(g for g in games if g.meta["server_game"] == 900)
    assert g1.suite == "server" and g1.map == "devil" and g1.seed == 255
    # The template replay names neither of our versions, so the seat comes from the slot winner:
    # slot a (us) won, the replay's winner is A, so we sat in A.
    assert (g1.a, g1.b, g1.winner) == (live.id, "server:C--", "A")
    assert g1.rounds == 68 and g1.reason == "elimination" and g1.stats["source"] == "replay"
    assert (arena.cfg.replays / g1.replay).read_bytes() == REPLAY       # stored uncompressed
    g2 = next(g for g in games if g.meta["server_game"] == 901)
    assert g2.replay is None and g2.meta["log"] == "team A timed out on round 3"
    assert (g2.a, g2.b, g2.winner) == (live.id, "server:C--", "B")       # slot b won, we're slot a
    assert arena.subs["server:C--"].status == "external"
    # the server suite: every map in play, and the seeds seen
    sdir = arena.cfg.map_dir("server")
    assert (sdir / "devil.map").read_text().startswith("MAP 16 16")
    assert (sdir / "prisoners_dilemma.map").exists()
    assert bridge_mod.seeds_for(arena, "devil") == [255]
    assert bridge_mod.seeds_for(arena, "prisoners_dilemma") == [16]
    # the queued battle waits; nothing is imported twice
    assert "950" not in bridge.imported["battles"]
    assert bridge.download() == 0
    assert len([g for g in arena.games if g.lane == "server"]) == 2
    summary = bridge.battle_summary(900)
    assert [x["result"] for x in summary["games"]] == ["W", "L"]
    assert summary["games"][0]["watch_url"] == f"/watch/{g1.id}"


def test_unknown_versions_become_stand_ins(bridge, arena):
    bridge.download()
    g = next(g for g in arena.games if g.lane == "server")
    assert g.a == "server-version:500"                  # the live one at the time, not in the arena
    assert arena.subs["server-version:500"].status == "external"


# ---------------------------------------------------------------------------- the gate
def _results(arena, cand, live, wins, losses, suite="official", lane="continuous", p99=10e6):
    st = {"teams": {t: {"points": {"p99": p99, "max": p99}, "errors": 0} for t in "AB"}}
    for i in range(wins + losses):
        a, b = (cand, live) if i % 2 else (live, cand)
        won = cand if i < wins else live
        arena.add_game(Game(id=0, lane=lane, suite=suite, map="one", seed=i, a=a, b=b,
                            winner="A" if won == a else "B", stats=st))


def _champion(arena, bridge, wins=60, losses=10):
    live = add(arena, "live")
    cand = add(arena, "cand", note="wider search")
    bridge.versions["500"] = {"arena": live.id}
    _results(arena, cand.id, live.id, wins, losses)
    _results(arena, cand.id, live.id, 2, 0, suite="holdout", lane="holdout")
    _results(arena, cand.id, live.id, 3, 1, suite="server", lane="placement")
    arena.ratings = rating.fit(arena)
    return cand, live


def test_gate_passes_and_uploads(bridge, arena, fake):
    cand, live = _champion(arena, bridge)
    fake.set("POST", "/api/v1/submissions", {"id": 777, "name": cand.id, "status": "building"})
    fake.set("POST", "/api/v1/submissions/777/activate", {"ok": True})
    g = bridge.gate()
    assert g["candidate"] == cand.id and g["passed"], g["failed"]
    bridge.auto_submit()
    up = fake.calls("POST", "/api/v1/submissions")
    assert len(up) == 2 and up[0][1] == "/api/v1/submissions" and up[1][1].endswith("/777/activate")
    body = up[0][3]
    assert b'name="name"\r\n\r\n' + cand.id.encode() in body and b'name="language"\r\n\r\ncpp' in body
    assert b"wider search" in body and b'filename="' in body
    assert up[0][4] == f"Bearer {KEY}"
    assert bridge.versions["777"]["arena"] == cand.id and bridge.versions["777"]["activated"]
    acts = [x["action"] for x in bridge.history()]
    assert "upload" in acts and "activate" in acts
    # within min_hours: no second upload
    bridge.auto_submit()
    assert len(fake.calls("POST", "/api/v1/submissions")) == 2
    assert any("last upload" in b for b in bridge.blocked())


def test_gate_holds_back_with_reasons(bridge, arena, fake):
    _champion(arena, bridge, wins=28, losses=27)
    g = bridge.gate()
    assert not g["passed"]
    names = {c["check"] for c in g["checks"] if not c["ok"]}
    assert "head_to_head" in names and "rating" in names
    bridge.auto_submit()
    assert not fake.calls("POST")
    held = bridge.history({"held_back"})
    assert held and held[0]["failed"]
    bridge.auto_submit()                                   # same reasons: logged once
    assert len(bridge.history({"held_back"})) == 1


def test_pause_blocks_upload(bridge, arena, fake):
    _champion(arena, bridge)
    bridge.pause("alice@github", "tournament tonight")
    bridge.auto_submit()
    assert not fake.calls("POST")
    assert bridge.history({"pause"})[0]["by"] == "alice@github"
    bridge.resume("bob@github")
    assert bridge.history({"resume"})[0]["by"] == "bob@github"


def test_auto_submit_off(bridge, arena, fake):
    arena.cfg.raw["server"]["auto_submit"] = False
    _champion(arena, bridge)
    bridge.auto_submit()
    assert not fake.calls("POST")
    assert "auto_submit is off in the config" in bridge.state["candidate"]["blocked"]


# ---------------------------------------------------------------------------- challenges
def test_challenge_checks_quota_first(bridge, fake):
    bridge.web = FakeWeb(remaining=3)
    with pytest.raises(Refused) as e:
        bridge.challenge("C--", ranked=True, maps=None, by="alice@github")
    assert e.value.status == 429 and e.value.extra["free_in_minutes"] == 12
    assert not fake.calls("POST")


def test_challenge_unranked(bridge, fake):
    fake.set("POST", "/api/v1/battles", {"ids": [1201]})
    r = bridge.challenge("#11", ranked=False, maps=["devil", "Prisoners"], by="alice@github")
    assert r["battles"] == [1201] and r["cost"] == 2
    post = fake.calls("POST", "/api/v1/battles")[0]
    assert json.loads(post[3]) == {"teamId": 852, "ranked": False, "mapIds": [13, 17]}
    rec = bridge.history({"challenge"})[0]
    assert rec["by"] == "alice@github" and rec["team"] == "C--"
    with pytest.raises(Refused):
        bridge.challenge("C", ranked=True, maps=None, by="x")        # ambiguous prefix
    with pytest.raises(Refused):
        bridge.challenge("UnderTheC", ranked=True, maps=None, by="x")  # ourselves


# ---------------------------------------------------------------------------- endpoints
def test_routes(bridge, arena, fake):
    from fastapi.testclient import TestClient

    from arena.api.http import create_app
    live = add(arena, "live")
    bridge.versions["500"] = {"arena": live.id}
    bridge.download()
    tc = TestClient(create_app(arena))
    texts = []

    def get(path, code=200):
        r = tc.get(path)
        texts.append(r.text)
        assert r.status_code == code, (path, r.text)
        return r.json()

    s = get("/api/v1/server")
    assert s["status"] == "ok" and s["live"]["arena"] == live.id and s["team"]["rank"] == 45
    assert s["quota"]["remaining"] == 60
    bs = get("/api/v1/server/battles")["battles"]
    b900 = next(b for b in bs if b["battle"] == 900)
    assert b900["games"][0]["arena_game"] and b900["games"][0]["watch_url"].startswith("/watch/")
    one = get("/api/v1/server/battles/900")
    assert one["us"]["team"] == "UnderTheC" and one["opponent"]["team"] == "C--"
    assert one["games"][1]["log"] == "team A timed out on round 3"
    gid = one["games"][0]["arena_game"]
    assert tc.get(f"/api/v1/games/{gid}/replay").content == REPLAY
    assert tc.get(f"/watch/{gid}").status_code == 200
    lb = get("/api/v1/server/leaderboard")
    assert lb["ours"]["rank"] == 45 and "members" not in lb["rows"][0]
    subs = get("/api/v1/server/submissions")
    assert subs[0]["live"] and subs[0]["arena"] == live.id and "buildLog" not in subs[0]
    assert get("/api/v1/server/ratings")["team"] == "UnderTheC"
    assert get("/api/v1/server/maps?text=false")["maps"][0]["file"] == "devil.map"
    assert get("/api/v1/server/quota")["remaining"] == 60
    r = tc.post("/api/v1/server/pause", json={"reason": "testing"})
    assert r.status_code == 200 and r.json()["paused"] and r.json()["paused_by"] == "local"
    assert tc.post("/api/v1/server/resume").json()["paused"] is False
    fake.set("POST", "/api/v1/battles", {"ids": [1300]})
    r = tc.post("/api/v1/server/battles", json={"team": "C--", "ranked": True})
    assert r.status_code == 200 and r.json()["battles"] == [1300]
    bridge.web = FakeWeb(remaining=0)
    r = tc.post("/api/v1/server/battles", json={"team": "C--", "ranked": True})
    assert r.status_code == 429 and "more free up in 12 min" in r.json()["detail"]
    get("/api/v1/server/teams/nobody", 404)
    team = get("/api/v1/server/teams/%2311")
    assert team["team"] == "C--" or team["team_id"] == 852
    for t in texts:
        assert KEY not in t and KEY[:12] not in t and "storageKey" not in t


def test_routes_without_key(arena):
    from fastapi.testclient import TestClient

    from arena.api.http import create_app
    arena.server_bridge = Bridge(arena, client=Client(None))
    tc = TestClient(create_app(arena))
    assert tc.get("/api/v1/server").json()["status"] == "no key configured"
    r = tc.get("/api/v1/server/leaderboard")
    assert r.status_code == 503 and r.json()["detail"] == "no key configured"


def test_link_server_version_relabels_games(tmp_path, monkeypatch):
    from arena import config
    from arena.server import bridge as bridge_mod
    from arena.state import Arena, Game, Submission
    cfg = config.Config(raw={"server": {"key_file": str(tmp_path / "nokey")}}, data_dir=tmp_path / "d")
    arena = Arena(cfg)
    s = Submission(id="gst-1", bot="gst", owner="alice", lane="ranked", status="rated", sha256="x")
    arena.subs[s.id] = s
    opp = arena.ensure_external("rivals")
    arena.add_game(Game(id=0, lane="server", suite="server", map="m", seed=1,
                        a="server-version:14246", b=opp.id, winner="A", rounds=10))
    b = bridge_mod.get(arena)
    monkeypatch.setattr(b, "server_subs", lambda: [{"id": 14246, "name": "gst-queen-3"}])
    out = b.link("14246", "gst-1", "alice")
    assert out["games_relabelled"] == 1 and arena.games[0].a == "gst-1"
    assert b.versions["14246"]["arena"] == "gst-1"


def test_force_upload_skips_the_gate_and_is_logged(bridge, arena, fake):
    from fastapi.testclient import TestClient

    from arena.api.http import create_app
    _champion(arena, bridge, wins=28, losses=27)            # a candidate the gate holds back
    assert not bridge.gate()["passed"]
    other = add(arena, "underdog")
    fake.set("POST", "/api/v1/submissions", {"id": 901, "name": other.id, "status": "building"})
    fake.set("POST", "/api/v1/submissions/901/activate", {"ok": True})
    c = TestClient(create_app(arena))
    r = c.post("/api/v1/server/upload", json={"submission": other.id})
    assert r.status_code == 200, r.text
    assert r.json()["server_version"] == 901 and bridge.versions["901"]["activated"]
    up = [x for x in bridge.history({"upload"})][0]
    assert up["submission"] == other.id and "(forced)" in up["by"] and up["evidence"]["forced"] is True
    assert c.post("/api/v1/server/upload", json={"submission": "nope-1"}).status_code == 404
    assert c.post("/api/v1/server/upload", json={}).status_code == 400
    fake.set("POST", "/api/v1/submissions", {"error": "zip too large"}, status=413)
    r = c.post("/api/v1/server/upload", json={"submission": other.id})
    assert r.status_code >= 400 and "zip too large" in r.text
    assert "Upload to the competition server now" in c.get(f"/s/{other.id}").text
