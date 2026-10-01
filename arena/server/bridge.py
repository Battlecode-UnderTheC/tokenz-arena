"""The server bridge (DESIGN.md section 8), on the competition server's key API.

    download()     polls GET /battles; each finished game becomes an arena game (lane and
                   suite 'server'), our seat holding the arena submission that was live and the
                   other seat the external bot server:<team>. The replay is saved under
                   replays/<game id>.replay (uncompressed, like the judge's) and read for the
                   verdict and stats; the battle's crash/timeout `log` goes in game.meta. Every
                   server map joins the `server` map suite (config.map_dir("server")) and every
                   seed seen goes in server/seeds.json.
    gate()         the champion gate: the candidate with its evidence and the failed conditions.
    auto_submit()  uploads and activates a candidate that passes, at most every min_hours,
                   unless paused or [server] auto_submit = false.

State, all under data_dir/server/ (atomic JSON writes):

    versions.json   server submission id -> {arena id, how we know, uploaded/activated}
    imported.json   server game id -> arena game id; battles fully imported; retry counts
    battles/<id>.json  one imported battle: opponent, kind, our side, each game
    seeds.json      map file stem -> [seeds seen on the server], first seen first
    maps.json       map file stem -> {name, server id, where the text came from}
    state.json      paused (by whom), last poll, last upload, the last gate result
    log.jsonl       every action: pauses, uploads, held-back candidates, challenges, errors

The bridge never crashes the arena: without a key it reports "no key configured".
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from arena import report, scheduler, store
from arena.server import replay
from arena.server.client import (DEFAULT_URL, Client, NoKey, ServerError, Web, finished,
                                 load_key)
from arena.state import EXTERNAL, Arena, Game, Submission, now, result_for

log = logging.getLogger("arena.server")

UPLOAD_MAX = 4 * 2**20                       # the server's zip limit
STAND_IN = "server-version:"                 # our server versions the arena has no record of


class Refused(ServerError):
    """A request the bridge itself turns down (quota, bad input), with an HTTP status."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message, status)
        self.extra = extra


def slug(name: str) -> str:
    """A server map name as a map file stem: 'Queen Of Spades' -> 'queen_of_spades'."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "map"


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _score(r: dict) -> float | None:
    n = r["W"] + r["D"] + r["L"]
    return round((r["W"] + r["D"] / 2) / n, 4) if n else None


def _wdl(games, sid) -> dict:
    out = {"W": 0, "D": 0, "L": 0}
    for g in games:
        r = result_for(g, sid)
        if r:
            out[r] += 1
    return out


class Bridge:
    def __init__(self, arena: Arena, client: Client | None = None, web: Web | None = None):
        self.arena = arena
        cfg = arena.cfg
        self.dir = cfg.data_dir / "server"
        self.url = self.setting("url", DEFAULT_URL)
        self.client = client or Client(load_key(self.setting("key_file"), self.url), self.url)
        self.web = web or Web()
        self.versions: dict = store.read_json(self.dir / "versions.json", {}) or {}
        self.imported: dict = store.read_json(self.dir / "imported.json", {}) or {}
        for k in ("games", "battles", "failures"):
            self.imported.setdefault(k, {})
        self.seeds: dict = store.read_json(self.dir / "seeds.json", {}) or {}
        self.maps: dict = store.read_json(self.dir / "maps.json", {}) or {}
        self.state: dict = store.read_json(self.dir / "state.json", {}) or {}
        self.lock = threading.RLock()                 # state files
        self._download = threading.Lock()             # one download at a time
        self._gate = threading.Lock()

    # ------------------------------------------------------------------ settings and files
    def setting(self, key: str, default=None):
        return self.arena.cfg.get("server", key, default)

    @property
    def configured(self) -> bool:
        return self.client.configured

    def _save(self, name: str):
        store.write_json(self.dir / f"{name}.json", getattr(self, name))

    def record(self, action: str, **fields):
        """Appends one line to server/log.jsonl."""
        line = {"at": now(), "action": action, **fields}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self.dir / "log.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
        log.info("server: %s %s", action, {k: v for k, v in fields.items() if k != "evidence"})

    def history(self, actions: set[str] | None = None, limit: int = 50) -> list[dict]:
        try:
            lines = (self.dir / "log.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not actions or d.get("action") in actions:
                out.append(d)
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------------------------------ who we are
    def our_team(self) -> tuple[int, str]:
        me, _ = self.client.me()
        team = me.get("team") or {}
        return int(team["id"]), team.get("name", "?")

    def server_subs(self) -> list[dict]:
        subs, _ = self.client.submissions()
        return subs if isinstance(subs, list) else []

    def live_version(self) -> dict | None:
        return next((s for s in self.server_subs() if s.get("status") == "active"), None)

    def arena_id(self, ver, sub: dict | None = None) -> str:
        """The arena submission a server version of ours is: versions.json, else a source hash
        that matches an arena upload, else a stand-in 'server-version:<id>'."""
        key = str(ver)
        with self.lock:
            hit = self.versions.get(key)
            if hit and hit.get("arena") and hit["arena"] in self.arena.subs:
                return hit["arena"]
        sub = sub or next((s for s in self.server_subs() if str(s.get("id")) == key), None)
        sha = (sub or {}).get("sourceHash")
        if sha and (match := self.arena.by_sha(sha)) and match.status != EXTERNAL:
            with self.lock:
                self.versions[key] = {"arena": match.id, "name": sub.get("name"), "by": "source hash",
                                      "at": now()}
                self._save("versions")
            return match.id
        return self._stand_in(key, sub)

    def _stand_in(self, ver: str, sub: dict | None) -> str:
        sid = f"{STAND_IN}{ver}"
        a = self.arena
        with a.lock:
            if sid not in a.subs:
                name = (sub or {}).get("name") or ver
                s = Submission(id=sid, bot=sid, owner="server", lane="external", status=EXTERNAL,
                               sha256=sid, language=(sub or {}).get("language"),
                               note=f"our server version {ver} ({name}), not an arena submission. "
                                    f"{((sub or {}).get('description') or '')[:500]}")
                a.subs[sid] = s
                a.save_sub(s)
        return sid

    def version_at(self, when: float | None, subs: list[dict]) -> dict | None:
        """Our version that was live at `when`: the latest activation we recorded before it,
        else the latest upload before it (the site keeps no activation history)."""
        if when is None:
            return None
        best, best_t = None, -1.0
        for s in subs:
            v = self.versions.get(str(s.get("id"))) or {}
            t = _ts(v.get("activated")) or _ts(s.get("uploadedAt"))
            if t is not None and best_t < t <= when:
                best, best_t = s, t
        return best

    # ------------------------------------------------------------------ the server suite
    def suite_dir(self) -> Path:
        return self.arena.cfg.map_dir("server") or (self.arena.cfg.data_dir / "maps" / "server")

    def _write_map(self, name: str, text: str, source: str, map_id=None, active=None) -> str:
        stem = slug(name)
        d = self.suite_dir()
        p = d / f"{stem}.map"
        known = self.maps.get(stem) or {}
        # Text from the API (with bed ranges) wins over text rebuilt from a replay (zeroed).
        if not p.exists() or (source == "api" and known.get("source") != "api") \
                or (source == "api" and p.read_text() != text):
            d.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".map.tmp")
            tmp.write_text(text if text.endswith("\n") else text + "\n")
            tmp.replace(p)
        entry = {**known, "name": name, "source": known.get("source") if known.get("source") == "api"
                 and source != "api" else source}
        if map_id is not None:
            entry["id"] = map_id
        if active is not None:
            entry["active"] = active
        if entry != known:
            self.maps[stem] = entry
            self._save("maps")
        return stem

    def sync_maps(self):
        """Every map in play (GET /maps) into the server suite."""
        maps, _ = self.client.maps()
        with self.lock:
            for m in maps if isinstance(maps, list) else []:
                if m.get("name") and m.get("mapText"):
                    self._write_map(m["name"], m["mapText"], "api", m.get("id"), m.get("active"))

    def add_seed(self, stem: str, seed: int):
        with self.lock:
            seen = self.seeds.setdefault(stem, [])
            if seed not in seen:
                seen.append(seed)
                self._save("seeds")

    # ------------------------------------------------------------------ download
    def download(self) -> int:
        """Imports new finished battles. Returns the number of games imported."""
        if not self.configured or not self._download.acquire(blocking=False):
            return 0
        n_games = 0
        try:
            team_id, _ = self.our_team()
            try:
                self.sync_maps()
            except ServerError as e:
                self.state["last_error"] = str(e)
            battles, _ = self.client.battles(self.setting("download_limit", 20))
            per_poll = int(self.setting("download_per_poll", 3))
            done = 0
            for b in reversed(battles if isinstance(battles, list) else []):     # oldest first
                if done >= per_poll:
                    break
                if str(b.get("id")) in self.imported["battles"]:
                    continue
                detail, _ = self.client.battle(b["id"])
                if not finished(detail):
                    continue
                got = self.import_battle(b, detail, team_id)
                if got is None:
                    continue
                n_games += got
                done += 1
            self.state.update(last_poll=now(), last_error=None)
        except NoKey:
            pass
        except ServerError as e:
            self.state.update(last_poll=now(), last_error=str(e))
            log.warning("server download: %s", e)
        finally:
            with self.lock:
                self._save("state")
            self._download.release()
        return n_games

    def import_battle(self, b: dict, detail: dict, team_id: int) -> int | None:
        """Imports a finished battle's games. None if it should be retried later."""
        a = self.arena
        m0 = detail.get("match") or {}
        bid = str(b.get("id") or m0.get("id"))
        our_slot = "a" if m0.get("teamAId") == team_id else "b"
        opp_name = detail.get("teamBName" if our_slot == "a" else "teamAName") or b.get("opponent") or "?"
        opp = a.ensure_external(opp_name).id
        subs = self.server_subs()
        our_ids = {str(s.get("id")) for s in subs}
        summary = {"id": int(bid), "at": b.get("at") or m0.get("completedAt"), "ranked": bool(m0.get("ranked")),
                   "kind": "ranked" if m0.get("ranked") else "unranked", "opponent": opp_name,
                   "opponent_id": m0.get("teamBId" if our_slot == "a" else "teamAId"),
                   "opponent_elo": detail.get("teamBElo" if our_slot == "a" else "teamAElo"),
                   "our_elo": detail.get("teamAElo" if our_slot == "a" else "teamBElo"),
                   "elo_change": m0.get("eloChangeA" if our_slot == "a" else "eloChangeB"),
                   "series": m0.get("seriesId"), "slot": our_slot, "games": []}
        new = 0
        for g in detail.get("games") or []:
            gid = str(g["id"])
            if gid in self.imported["games"]:
                summary["games"].append(self._game_row(int(self.imported["games"][gid])))
                continue
            gd = detail if gid == str(m0.get("id")) else self.client.battle(int(gid))[0]
            m = gd.get("match") or {}
            blob = None
            if g.get("hasReplay", True) and g.get("status") == "completed":
                try:
                    blob = self.client.replay(int(gid))
                except ServerError as e:
                    tries = self.imported["failures"].get(gid, 0) + 1
                    self.imported["failures"][gid] = tries
                    self._save("imported")
                    if tries < 3:
                        log.warning("server replay %s: %s (try %d)", gid, e, tries)
                        return None
            dec = None
            if blob:
                try:
                    dec = replay.load(blob)
                except Exception as e:      # an unexpected replay format: import without stats
                    log.warning("server replay %s unreadable: %s", gid, e)
            game = self._game(m, g, dec, our_slot, our_ids, subs, opp, summary)
            a.add_game(game)
            if blob:
                a.cfg.replays.mkdir(parents=True, exist_ok=True)
                (a.cfg.replays / f"{game.id}.replay").write_bytes(replay.gunzip(blob))
                game.replay = f"{game.id}.replay"
                a.save_game(game)
            with self.lock:
                self.imported["games"][gid] = game.id
                self._save("imported")
            summary["games"].append(self._game_row(game.id))
            new += 1
        res = [x.get("result") for x in summary["games"]]
        summary["wins"], summary["losses"] = res.count("W"), res.count("L")
        summary["draws"] = res.count("D")
        summary["outcome"] = b.get("outcome") or ("win" if summary["wins"] > summary["losses"]
                                                 else "loss" if summary["wins"] < summary["losses"]
                                                 else "draw")
        with self.lock:
            store.write_json(self.dir / "battles" / f"{bid}.json", summary)
            self.imported["battles"][bid] = {"games": len(summary["games"]), "at": summary["at"]}
            self._save("imported")
        return new

    def _game(self, m: dict, g: dict, dec: dict | None, our_slot: str, our_ids: set, subs: list,
              opp: str, summary: dict) -> Game:
        winner_slot = m.get("winner") or g.get("winner")
        seat, ver = None, None
        if dec:
            for s_, bot in (("A", dec["botA"]), ("B", dec["botB"])):
                if bot in our_ids:
                    seat, ver = s_, bot
            if seat is None and dec["result"]["winner"] and winner_slot in ("a", "b"):
                # the replay names neither bot: slot a's seat is where the slot winner sat
                rw = dec["result"]["winner"]
                seat_a = rw if winner_slot == "a" else ("B" if rw == "A" else "A")
                seat = seat_a if our_slot == "a" else ("B" if seat_a == "A" else "A")
        seat = seat or our_slot.upper()
        if ver is None:
            live = self.version_at(_ts(m.get("requestedAt") or m.get("startedAt")), subs)
            ver = str(live["id"]) if live else "unknown"
        sub = next((s for s in subs if str(s.get("id")) == ver), None)
        ours = self.arena_id(ver, sub)
        name = g.get("mapName") or "unknown"
        text = dec["map"] if dec else ""
        stem = slug(name)
        with self.lock:
            if text and not (self.suite_dir() / f"{stem}.map").exists():
                self._write_map(name, text, "replay")
        seed = None
        if m.get("seed"):
            try:
                seed = int(str(m["seed"]), 16)
            except ValueError:
                seed = None
        if seed is None and dec:
            seed = dec.get("seed")
        if seed is not None:
            self.add_seed(stem, seed)
        status = m.get("status") or g.get("status")
        game = Game(id=0, lane="server", suite="server", map=stem, seed=seed or 0,
                    a=ours if seat == "A" else opp, b=opp if seat == "A" else ours)
        if dec:
            s = replay.summarize(dec)
            game.winner, game.rounds, game.reason, game.stats = s["winner"], s["rounds"], s["reason"], s["stats"]
        else:
            other = "B" if seat == "A" else "A"
            game.winner = seat if winner_slot == our_slot else other if winner_slot in ("a", "b") else None
            game.reason = f"server: {status}"
            game.stats = {"source": "api", "scores": {seat: m.get(f"scores{our_slot.upper()}"),
                                                      other: m.get("scoresB" if our_slot == "a" else "scoresA")}}
            if winner_slot not in ("a", "b") and status != "completed":
                game.error = f"server game {status}"
        t0, t1 = _ts(m.get("startedAt")), _ts(m.get("completedAt"))
        game.seconds = round(t1 - t0, 1) if t0 and t1 else None
        game.played = _iso(t1) if t1 else now()
        game.meta = {"server_battle": summary["id"], "server_game": int(g["id"]), "series": summary["series"],
                     "ranked": summary["ranked"], "opponent": summary["opponent"],
                     "opponent_id": summary["opponent_id"], "our_seat": seat, "our_version": ver,
                     "map_name": name, "map_id": m.get("mapId"), "status": status,
                     "log": m.get("log"), "requested": m.get("requestedAt"),
                     "elo_change": summary["elo_change"]}
        return game

    def _game_row(self, gid: int) -> dict:
        """One game of a battle, from our side, linked to the arena game."""
        g = next((x for x in reversed(self.arena.games) if x.id == gid), None)
        if g is None:
            return {"arena_game": gid}
        seat = g.meta.get("our_seat") or "A"
        ours = g.a if seat == "A" else g.b
        st = (g.stats or {}).get("standings") or {}
        return {"game": g.meta.get("server_game"), "map": g.meta.get("map_name") or g.map,
                "arena_game": g.id, "our_seat": seat, "us": ours, "result": result_for(g, ours),
                "winner": g.winner, "rounds": g.rounds, "reason": g.reason,
                "totals": {"us": (st.get(seat) or {}).get("total"),
                           "them": (st.get("B" if seat == "A" else "A") or {}).get("total")},
                "log": g.meta.get("log"),
                "replay_url": f"/api/v1/games/{g.id}/replay" if g.replay else None,
                "watch_url": f"/watch/{g.id}" if g.replay else None}

    def battle_summary(self, bid: int) -> dict | None:
        return store.read_json(self.dir / "battles" / f"{int(bid)}.json")

    # ------------------------------------------------------------------ the gate
    def gate(self) -> dict:
        """The champion gate (DESIGN.md 8.3): the candidate, the evidence, what failed."""
        a = self.arena
        h2h_min = float(self.setting("head_to_head_min", 0.55))
        h2h_games = int(self.setting("head_to_head_games", 60))
        cpu_max = float(self.setting("cpu_p99_max", 70_000_000))
        out: dict = {"at": now(), "candidate": None, "live": None, "passed": False, "failed": [],
                     "checks": []}
        live_ver = None
        try:
            lv = self.live_version()
            live_ver = str(lv["id"]) if lv else None
        except ServerError as e:
            out["failed"].append(f"can't read the live version: {e}")
            return out
        live = self.arena_id(live_ver, lv) if live_ver else None
        out["live"] = {"server_version": live_ver, "name": (lv or {}).get("name"), "arena": live}
        ranking = scheduler.ranking(a)
        if not ranking:
            out["failed"].append("no ranked submissions on the arena")
            return out
        cand = ranking[0]
        if cand == live:
            out["failed"].append(f"the live bot ({cand}) is already #1 on the arena")
            return out
        out["candidate"] = cand
        ratings = getattr(a, "ratings", {}) or {}
        checks = out["checks"]

        def check(name, ok, detail, **evidence):
            checks.append({"check": name, "ok": bool(ok), "detail": detail, **evidence})
            if not ok:
                out["failed"].append(f"{name}: {detail}")

        live_known = bool(live) and not live.startswith(STAND_IN) and live in a.subs
        rc, rl = ratings.get(cand) or {}, ratings.get(live) or {} if live_known else {}
        if not live_known:
            check("rating", False, f"the live server version {live_ver} isn't an arena submission, so "
                  "there's nothing to compare against (add it to data_dir/server/versions.json or "
                  "submit its zip to the arena)")
        elif rc.get("elo") is None or rl.get("elo") is None:
            check("rating", False, "the candidate or the live bot has no arena rating yet",
                  candidate=rc or None, live=rl or None)
        else:
            lo, hi = rc["elo"] - (rc.get("err") or 1e9), rl["elo"] + (rl.get("err") or 1e9)
            check("rating", lo > hi, f"#1 at {rc['elo']}±{rc.get('err')}; lower bound {lo:.0f} vs the "
                  f"live bot's upper bound {hi:.0f} ({rl['elo']}±{rl.get('err')})",
                  candidate=rc, live=rl)
        with a.lock:
            games = [g for g in a.games_of(cand) if g.a != g.b and g.lane != "server"]
        if live_known:
            vs = [g for g in games if live in (g.a, g.b) and g.suite not in ("check", "holdout")]
            r = _wdl(vs, cand)
            n, sc = sum(r.values()), _score(r)
            check("head_to_head", n >= h2h_games and sc is not None and sc >= h2h_min,
                  f"{r['W']}-{r['D']}-{r['L']} vs {live} ({n} games, "
                  f"{'-' if sc is None else f'{100 * sc:.1f}%'}); needs ≥{100 * h2h_min:.0f}% over "
                  f"≥{h2h_games}", record=r)
        for suite, name in (("holdout", "holdout"), ("server", "server_suite")):
            mine = [g for g in games if g.suite == suite]
            rc_ = _wdl(mine, cand)
            sc = _score(rc_)
            if sc is None:
                check(name, False, f"no {suite} games yet", record=rc_)
                continue
            sl = None
            if live_known:
                with a.lock:
                    theirs = [g for g in a.games_of(live) if g.suite == suite and g.lane != "server"
                              and g.a != g.b]
                sl = _score(_wdl(theirs, live))
            bar = sl if sl is not None else 0.5
            check(name, sc >= bar, f"{100 * sc:.1f}% vs the live bot's "
                  f"{'-' if sl is None else f'{100 * sl:.1f}%'} (bar {100 * bar:.1f}%)",
                  record=rc_, live_score=sl)
        rep = report.build(a, cand)
        check("errors", rep["errors"] == 0 and a.subs[cand].status not in ("rejected",),
              f"{rep['errors']} errors in its games", errors=rep["errors"])
        p99 = (rep.get("cpu") or {}).get("p99_max")
        check("cpu", p99 is not None and p99 < cpu_max,
              f"CPU p99 {'unknown' if p99 is None else f'{p99 / 1e6:.1f}M'}; needs < {cpu_max / 1e6:.0f}M",
              p99=p99)
        src = a.cfg.submissions / cand / "source.zip"
        size = src.stat().st_size if src.exists() else None
        check("upload", size is not None and size <= UPLOAD_MAX and bool(a.subs[cand].language),
              "no source zip" if size is None else f"zip {size / 2**20:.2f} MB (server limit 4 MB), "
              f"language {a.subs[cand].language}")
        out["passed"] = not out["failed"]
        return out

    def blocked(self) -> list[str]:
        """Why auto-submit wouldn't upload right now, gate aside."""
        out = []
        if not self.configured:
            out.append("no key configured")
        if not self.setting("auto_submit", True):
            out.append("auto_submit is off in the config")
        if self.state.get("paused"):
            out.append(f"paused by {self.state.get('paused_by')} at {self.state.get('paused_at')}")
        nxt = self.next_upload_at()
        if nxt and nxt > time.time():
            out.append(f"the last upload was under {self.setting('min_hours', 4)} h ago; next at {_iso(nxt)}")
        return out

    def next_upload_at(self) -> float | None:
        last = _ts(self.state.get("last_upload"))
        failed = _ts(self.state.get("last_failure"))
        t = [x for x in (last and last + float(self.setting("min_hours", 4)) * 3600,
                         failed and failed + 3600) if x]
        return max(t) if t else None

    def auto_submit(self):
        """The gate, then an upload if it passed and nothing blocks it."""
        if not self._gate.acquire(blocking=False):
            return
        try:
            self._retry_activation()
            g = self.gate()
            g["blocked"] = self.blocked()
            sig = [g["candidate"], sorted(g["failed"]) if not g["passed"] else "passed", g["blocked"]]
            if g["candidate"] and sig != self.state.get("held_sig") and not (g["passed"] and not g["blocked"]):
                self.record("held_back", candidate=g["candidate"], live=(g["live"] or {}).get("arena"),
                            failed=g["failed"], blocked=g["blocked"])
            self.state["held_sig"] = sig
            self.state["candidate"] = g
            if g["passed"] and not g["blocked"]:
                self.upload(g["candidate"], by="auto-submit", evidence=g["checks"])
        finally:
            with self.lock:
                self._save("state")
            self._gate.release()

    def upload(self, sid: str, by: str, evidence=None) -> dict | None:
        """POST /submissions with the arena submission's zip, then activate it."""
        a = self.arena
        s = a.subs[sid]
        blob = (a.cfg.submissions / sid / "source.zip").read_bytes()
        self.state["last_attempt"] = now()
        try:
            r = self.client.upload(name=sid, language=s.language or "cpp",
                                   description=(s.note or "")[:2000], zip_bytes=blob,
                                   filename=f"{sid}.zip")
            sub = r.get("submission") if isinstance(r.get("submission"), dict) else r
            ver = sub.get("id") or r.get("submissionId")
            if ver is None:
                raise ServerError("the upload answered without a submission id")
        except ServerError as e:
            self.state["last_failure"] = now()
            self.record("upload_failed", submission=sid, by=by, error=str(e))
            return None
        with self.lock:
            self.versions[str(ver)] = {"arena": sid, "name": sid, "by": by, "uploaded": now(),
                                       "activated": None}
            self._save("versions")
        self.record("upload", submission=sid, server_version=ver, by=by, evidence=evidence)
        self.state["last_upload"] = now()
        self._activate(str(ver), by)
        return {"server_version": ver, "submission": sid}

    def _activate(self, ver: str, by: str) -> bool:
        try:
            self.client.activate(int(ver))
        except ServerError as e:
            with self.lock:
                self.versions[ver]["activation_error"] = str(e)
                self._save("versions")
            self.record("activate_failed", server_version=ver, by=by, error=str(e))
            return False
        with self.lock:
            self.versions[ver]["activated"] = now()
            self.versions[ver].pop("activation_error", None)
            self._save("versions")
        self.record("activate", server_version=ver, submission=self.versions[ver].get("arena"), by=by)
        return True

    def _retry_activation(self):
        """An upload whose activation failed (still building, say) is retried for an hour."""
        for ver, v in list(self.versions.items()):
            t = _ts(v.get("uploaded"))
            if v.get("by") == "auto-submit" and not v.get("activated") and t and time.time() - t < 3600:
                self._activate(ver, "auto-submit (retry)")

    def link(self, ver, arena_id: str, by: str) -> dict:
        """Records that our server version `ver` is arena submission `arena_id` (for versions
        uploaded by hand rather than by auto-submit), and re-labels the server games already
        downloaded for it, so the gate and the pages know which bot is live."""
        ver = str(ver)
        s = self.arena.subs.get(arena_id)
        if not s or s.status == "external":
            raise Refused(f"no arena submission {arena_id!r}", 404)
        names = {str(x.get("id")): x.get("name") for x in self.server_subs()}
        if ver not in names:
            raise Refused(f"no server version {ver} of ours (see GET /api/v1/server/submissions)", 404)
        stand_in = f"{STAND_IN}{ver}"
        with self.lock:
            self.versions[ver] = {**self.versions.get(ver, {}), "arena": arena_id,
                                  "name": names[ver], "by": f"linked by {by}", "linked": now()}
            self._save("versions")
        moved = 0
        with self.arena.lock:
            for g in self.arena.games:
                if stand_in in (g.a, g.b):
                    g.a = arena_id if g.a == stand_in else g.a
                    g.b = arena_id if g.b == stand_in else g.b
                    self.arena.save_game(g)
                    moved += 1
        self.record("link", server_version=ver, submission=arena_id, by=by, games=moved)
        return {"server_version": ver, "name": names[ver], "arena": arena_id, "games_relabelled": moved}

    def pause(self, by: str, reason: str | None = None):
        with self.lock:
            self.state.update(paused=True, paused_by=by, paused_at=now(), pause_reason=reason)
            self._save("state")
        self.record("pause", by=by, reason=reason)

    def resume(self, by: str):
        with self.lock:
            self.state.update(paused=False, resumed_by=by, resumed_at=now())
            self._save("state")
        self.record("resume", by=by)

    # ------------------------------------------------------------------ teams, quota, challenges
    def resolve_team(self, arg: str) -> dict:
        """{id, name, rank, elo} for an id ('123' or 'id:123'), '#rank', or a name (exact, then a
        unique prefix, case-insensitive), from the ratings ladder."""
        s = str(arg).strip()
        ladder, _ = self.client.ratings()
        rows = (ladder or {}).get("ladder") or []
        pick = lambda r: {"id": r.get("id"), "name": r.get("name"), "rank": r.get("rank"),
                          "elo": r.get("elo")}
        if s.lower().startswith("id:") or s.isdigit():
            tid = int(s[3:] if s.lower().startswith("id:") else s)
            row = next((r for r in rows if r.get("id") == tid), None)
            return pick(row) if row else {"id": tid, "name": None, "rank": None, "elo": None}
        if s.startswith("#") and s[1:].isdigit():
            row = next((r for r in rows if r.get("rank") == int(s[1:])), None)
            if not row:
                raise Refused(f"no team at rank {s}", 404)
            return pick(row)
        exact = [r for r in rows if str(r.get("name", "")).lower() == s.lower()]
        if len(exact) == 1:
            return pick(exact[0])
        pref = [r for r in rows if str(r.get("name", "")).lower().startswith(s.lower())]
        if len(pref) == 1:
            return pick(pref[0])
        if not pref:
            raise Refused(f"no team named {arg!r}", 404)
        raise Refused(f"{arg!r} is ambiguous: " + ", ".join(r["name"] for r in pref[:10]), 400)

    def quota(self, team_id: int | None = None) -> dict:
        """Games left this hour for challenges (the site's availability, through bcweb), plus
        our own record of recent challenges."""
        tid = team_id or self.our_team()[0]
        av = self.web.availability(tid)
        recent = self.history({"challenge"}, limit=20)
        cut = time.time() - 3600
        last_hour = [r for r in recent if (_ts(r.get("at")) or 0) >= cut]
        out = {"team_id": tid, "remaining": None, "free_in_minutes": None, "dev_pool": None,
               "source": "site", "recent_challenges": recent,
               "games_challenged_last_hour": sum(r.get("cost", 0) for r in last_hour)}
        if av:
            out.update(remaining=av.get("remaining"), free_in_minutes=av.get("freeIn"),
                       dev_pool=av.get("dev"))
        else:
            out.update(source="arena log (the site's count is unavailable: "
                              f"{self.web.last_error or 'no web login'})",
                       remaining=max(0, 60 - out["games_challenged_last_hour"]))
        return out

    def challenge(self, team: str, ranked: bool, maps: list[str] | None, by: str) -> dict:
        t = self.resolve_team(team)
        our_id, _ = self.our_team()
        if t["id"] == our_id:
            raise Refused("that's our own team", 400)
        chosen = []
        if ranked and maps:
            raise Refused("ranked battles are 5 games on random maps; maps are for unranked only", 400)
        if not ranked:
            all_maps, _ = self.client.maps()
            active = [m for m in all_maps if m.get("active", True)] if isinstance(all_maps, list) else []
            for w in maps or []:
                m = next((m for m in active if m["name"].lower() == w.lower() or slug(m["name"]) == slug(w)),
                         None) or next((m for m in active if m["name"].lower().startswith(w.lower())), None)
                if not m:
                    raise Refused(f"unknown map {w!r}; maps in play: " + ", ".join(m["name"] for m in active), 400)
                if m not in chosen:
                    chosen.append(m)
            if len(chosen) > 10:
                raise Refused("an unranked battle takes at most 10 maps", 400)
        cost = 5 if ranked else (len(chosen) or len([m for m in (self.client.peek("maps") or [])
                                                     if m.get("active", True)][:10]) or 10)
        q = self.quota(t["id"])
        rem = q.get("remaining")
        if rem is not None and cost > rem:
            raise Refused(f"not enough quota: this needs {cost} games and {rem} are left this hour; "
                          f"more free up in {q.get('free_in_minutes')} min", 429,
                          remaining=rem, cost=cost, free_in_minutes=q.get("free_in_minutes"))
        r = self.client.challenge(t["id"], ranked, [m["id"] for m in chosen])
        ids = r.get("ids") or ([r["id"]] if r.get("id") else [])
        self.record("challenge", by=by, team=t["name"], team_id=t["id"], ranked=ranked,
                    maps=[m["name"] for m in chosen], cost=cost, battles=ids)
        return {"battles": ids, "team": t, "ranked": ranked, "maps": [m["name"] for m in chosen],
                "cost": cost, "by": by, "quota_before": rem}

    # ------------------------------------------------------------------ summary
    def summary(self) -> dict:
        out: dict = {"configured": self.configured, "server": self.url,
                     "auto_submit": {"enabled": bool(self.setting("auto_submit", True)),
                                     "paused": bool(self.state.get("paused")),
                                     "paused_by": self.state.get("paused_by"),
                                     "paused_at": self.state.get("paused_at"),
                                     "min_hours": self.setting("min_hours", 4),
                                     "last_upload": self.state.get("last_upload"),
                                     "next_upload_at": (_iso(n) if (n := self.next_upload_at()) else None),
                                     "candidate": self.state.get("candidate")},
                     "downloads": {"games": len(self.imported["games"]),
                                   "battles": len(self.imported["battles"]),
                                   "last_poll": self.state.get("last_poll"),
                                   "last_error": self.state.get("last_error")},
                     "uploads": self.history({"upload", "upload_failed", "activate", "activate_failed"}, 10)}
        if not self.configured:
            out["status"] = "no key configured"
            return out
        out["status"] = "ok"
        stale = False
        try:
            team, stale = self.client.team()
            t = team.get("team") or {}
            out["team"] = {"id": t.get("id"), "name": t.get("name"), "rank": team.get("rank"),
                           "elo": t.get("elo"), "peak": team.get("peak"), "record": team.get("record")}
            lv = self.live_version()
            if lv:
                out["live"] = {"server_version": lv["id"], "name": lv.get("name"),
                               "arena": self.arena_id(str(lv["id"]), lv),
                               "uploaded": lv.get("uploadedAt"),
                               "record": {"W": lv.get("wins"), "D": lv.get("draws"), "L": lv.get("losses")},
                               "rating": lv.get("rating")}
        except ServerError as e:
            out["status"] = f"server unavailable: {e}"
        q = self.quota() if out["status"] == "ok" else None
        if q:
            out["quota"] = {k: q[k] for k in ("remaining", "free_in_minutes", "source")}
        out["stale"] = stale
        return out


# ---------------------------------------------------------------------------- wiring
def get(arena: Arena) -> Bridge:
    """The arena's bridge (made on first use when `arena serve` didn't install one)."""
    b = getattr(arena, "server_bridge", None)
    if b is None:
        b = arena.server_bridge = Bridge(arena)
    return b


def seeds_for(arena: Arena, stem: str) -> list[int]:
    """The seeds seen on the server for a server-suite map (file stem), first seen first."""
    return list((store.read_json(arena.cfg.data_dir / "server" / "seeds.json", {}) or {}).get(stem, []))


def install(arena: Arena, daemon) -> Bridge:
    """Called by `arena serve`: polls the server for battles and runs the gate periodically.
    Without a key it only reports "no key configured"."""
    b = get(arena)
    if not b.configured:
        log.warning("server bridge: no key configured (set [server] key_file); not polling")
        return b
    daemon.every(float(b.setting("poll_seconds", 60)), b.download, "server-download")
    daemon.every(float(b.setting("gate_seconds", 300)), b.auto_submit, "server-gate")
    return b
