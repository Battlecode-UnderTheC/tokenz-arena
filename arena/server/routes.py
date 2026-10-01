"""The competition server as the arena's own endpoints (DESIGN.md section 8.1), under
/api/v1/server. Small flat JSON, our side already worked out, server games linked to arena
game ids and their replays. Mounted by arena/api/http.py via make_router(arena).

Responses read through the bridge's cached client, so many readers cost the server one
request. A response built from a cached copy after a 429 carries "stale": true.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from arena.server import bridge as bridge_mod
from arena.server.bridge import Bridge, Refused, slug
from arena.server.client import NoKey, RateLimited, ServerError
from arena.state import result_for


def _caller(request: Request) -> str:
    from arena.api.http import caller
    return caller(request).owner


def _fail(e: ServerError):
    if isinstance(e, NoKey):
        return JSONResponse({"detail": "no key configured", "configured": False}, status_code=503)
    if isinstance(e, RateLimited):
        return JSONResponse({"detail": str(e), "retry_after": round(e.retry_after)}, status_code=429,
                            headers={"Retry-After": str(max(1, round(e.retry_after)))})
    if isinstance(e, Refused):
        return JSONResponse({"detail": str(e), **e.extra}, status_code=e.status)
    status = e.status if e.status in (400, 403, 404, 409, 422) else 502
    return JSONResponse({"detail": str(e)}, status_code=status)


def make_router(arena) -> APIRouter:
    r = APIRouter(prefix="/api/v1/server")

    def br() -> Bridge:
        return bridge_mod.get(arena)

    def guarded(fn):
        """Turns the bridge's ServerErrors into JSON errors."""
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ServerError as e:
                return _fail(e)
        return wrapper

    # ------------------------------------------------------------------ summary
    @r.get("")
    def summary():
        return br().summary()

    # ------------------------------------------------------------------ ladder
    @r.get("/leaderboard")
    @guarded
    def leaderboard(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200)):
        b = br()
        rows, stale = b.client.leaderboard()
        our_id, _ = b.our_team()
        rows = rows if isinstance(rows, list) else []

        def row(t):
            return {"rank": t.get("rank"), "team_id": t.get("id"), "team": t.get("name"),
                    "elo": t.get("elo"), "wins": t.get("wins"), "language": t.get("language"),
                    "ranked": t.get("ranked"), "dev": t.get("dev"),
                    "university": t.get("university"), "eligible": t.get("eligible"),
                    "ours": t.get("id") == our_id}
        start = (page - 1) * per_page
        ours = next((row(t) for t in rows if t.get("id") == our_id), None)
        return {"page": page, "per_page": per_page, "teams": len(rows), "ours": ours,
                "rows": [row(t) for t in rows[start:start + per_page]], "stale": stale}

    @r.get("/ratings")
    @guarded
    def ratings(team: str | None = None):
        b = br()
        data, stale = b.client.ratings()
        t = b.resolve_team(team) if team else None
        tid = t["id"] if t else b.our_team()[0]
        row = next((x for x in (data or {}).get("ladder") or [] if x.get("id") == tid), {})
        hist = ((data or {}).get("histories") or {}).get(str(tid)) or []
        return {"team_id": tid, "team": row.get("name") or (t or {}).get("name"),
                "rank": row.get("rank"), "elo": row.get("elo"),
                "history": [{"at": h.get("date"), "elo": h.get("elo")} for h in hist], "stale": stale}

    @r.get("/teams/{team}")
    @guarded
    def team(team: str):
        b = br()
        t = b.resolve_team(team)
        prof, stale = b.client.team_profile(t["id"])
        p = prof.get("team") or {}
        return {"team_id": p.get("id"), "team": p.get("name"), "rank": prof.get("rank"),
                "elo": p.get("elo"), "peak": prof.get("peak"), "record": prof.get("record"),
                "description": p.get("description"), "skin": p.get("skin"), "dev": p.get("dev"),
                "ranked": p.get("ranked"), "has_bot": p.get("hasBot"),
                "members": [{"username": m.get("username"), "institution": m.get("institution")}
                            for m in p.get("members") or []],
                "accepts": {k: v for k, v in (p.get("settings") or {}).items() if k in ("ranked", "unranked")},
                "history": [{"at": h.get("date"), "elo": h.get("elo")} for h in prof.get("history") or []],
                "recent": [_battle_row(x, None) for x in prof.get("recent") or []],
                "arena_bot": f"server:{p.get('name')}" if f"server:{p.get('name')}" in arena.subs else None,
                "stale": stale}

    # ------------------------------------------------------------------ our versions
    @r.get("/submissions")
    @guarded
    def submissions():
        b = br()
        subs = b.server_subs()
        team, _ = b.client.team()
        members = {m.get("id"): m.get("username") for m in (team.get("team") or {}).get("members") or []}
        out = []
        for s in subs:
            aid = b.arena_id(str(s["id"]), s)
            local = [g for g in arena.games if g.lane == "server" and g.meta.get("our_version") == str(s["id"])]
            out.append({"server_version": s["id"], "name": s.get("name"), "version": s.get("version"),
                        "live": s.get("status") == "active", "status": s.get("status"),
                        "language": s.get("language"), "uploaded": s.get("uploadedAt"),
                        "uploaded_by": members.get(s.get("uploadedBy"), s.get("uploadedBy")),
                        "arena": None if aid.startswith(bridge_mod.STAND_IN) else aid,
                        "arena_stand_in": aid if aid.startswith(bridge_mod.STAND_IN) else None,
                        "record": {"W": s.get("wins"), "D": s.get("draws"), "L": s.get("losses")},
                        "rating": s.get("rating"),
                        "by_map": {name: {"W": m.get("wins"), "D": m.get("draws"), "L": m.get("losses"),
                                          "rating": m.get("rating")}
                                   for name, m in (s.get("byMap") or {}).items()},
                        "imported_games": len(local),
                        "description": (s.get("description") or "")[:300]})
        return out

    # ------------------------------------------------------------------ battles
    @r.get("/battles")
    @guarded
    def battles(kind: str | None = None, opponent: str | None = None, result: str | None = None,
                limit: int = Query(20, ge=1, le=200)):
        b = br()
        # the same list size the bridge polls with, so this is usually a cache hit
        rows, stale = b.client.battles(max(limit, int(b.setting("download_limit", 20))))
        out = []
        for x in rows if isinstance(rows, list) else []:
            if kind and kind != ("ranked" if x.get("ranked") else "unranked"):
                continue
            if opponent and opponent.lower() not in str(x.get("opponent", "")).lower():
                continue
            if result and not str(x.get("outcome") or "").lower().startswith(result.lower()[:1]):
                continue
            out.append(_battle_row(x, b.battle_summary(x["id"])))
            if len(out) >= limit:
                break
        return {"battles": out, "stale": stale}

    @r.get("/battles/{bid}")
    @guarded
    def battle(bid: int):
        b = br()
        d, stale = b.client.battle(bid)
        m = d.get("match") or {}
        our_id, _ = b.our_team()
        slot = "a" if m.get("teamAId") == our_id else "b" if m.get("teamBId") == our_id else None
        other = {"a": "b", "b": "a"}.get(slot or "", None)
        imported = b.battle_summary(bid) or {}
        by_game = {x.get("game"): x for x in imported.get("games") or []}
        games = []
        for g in d.get("games") or []:
            row = by_game.get(g["id"])
            if row is None:
                gid = b.imported["games"].get(str(g["id"]))
                row = b._game_row(int(gid)) if gid else None
            if row is None:
                gd, _ = b.client.battle(g["id"]) if g["id"] != m.get("id") else (d, False)
                gm = gd.get("match") or {}
                w = gm.get("winner") or g.get("winner")
                row = {"game": g["id"], "map": g.get("mapName"), "arena_game": None, "status": g.get("status"),
                       "result": None if not slot or w not in ("a", "b") else ("W" if w == slot else "L"),
                       "totals": {"us": gm.get(f"scores{slot.upper()}") if slot else None,
                                  "them": gm.get(f"scores{other.upper()}") if other else None},
                       "log": gm.get("log"), "seed": gm.get("seed"),
                       "replay_url": None, "watch_url": None}
            games.append(row)
        return {"battle": m.get("id"), "ranked": m.get("ranked"),
                "kind": "ranked" if m.get("ranked") else "unranked", "status": m.get("status"),
                "ours": slot is not None,
                "us": {"slot": slot, "team": d.get(f"team{slot.upper()}Name") if slot else None,
                       "elo": d.get(f"team{slot.upper()}Elo") if slot else None,
                       "elo_change": m.get(f"eloChange{slot.upper()}") if slot else None},
                "opponent": {"slot": other, "team": d.get(f"team{other.upper()}Name") if other else None,
                             "team_id": m.get(f"team{other.upper()}Id") if other else None,
                             "elo": d.get(f"team{other.upper()}Elo") if other else None},
                "teams": {"a": d.get("teamAName"), "b": d.get("teamBName")},
                "requested": m.get("requestedAt"), "completed": m.get("completedAt"),
                "queue": d.get("wait"), "log": m.get("log"), "games": games, "stale": stale}

    @r.post("/battles")
    @guarded
    def challenge(request: Request, body: dict = Body(...)):
        team = body.get("team")
        if not team:
            raise HTTPException(400, "give the team: an id, a name (unique prefix) or #rank")
        maps = body.get("maps") or []
        if isinstance(maps, str):
            maps = [m.strip() for m in maps.split(",") if m.strip()]
        return br().challenge(str(team), bool(body.get("ranked", False)), maps, by=_caller(request))

    # ------------------------------------------------------------------ quota, maps, queue
    @r.get("/quota")
    @guarded
    def quota():
        return br().quota()

    @r.get("/maps")
    @guarded
    def maps(text: bool = True):
        b = br()
        rows, stale = b.client.maps()
        out = []
        for m in rows if isinstance(rows, list) else []:
            stem = slug(m.get("name", ""))
            games = [g for g in arena.games if g.suite == "server" and g.map == stem and not g.error]
            real = [g for g in games if g.lane == "server"]
            us = {"W": 0, "D": 0, "L": 0}
            for g in real:
                seat = g.meta.get("our_seat") or "A"
                res = result_for(g, g.a if seat == "A" else g.b)
                if res:
                    us[res] += 1
            row = {"map_id": m.get("id"), "name": m.get("name"), "file": f"{stem}.map",
                   "size": m.get("sizeLabel"), "active": m.get("active"),
                   "seeds_seen": len(b.seeds.get(stem, [])),
                   "server_games": len(real), "our_server_record": us,
                   "arena_games": len(games) - len(real),
                   "first_seat_wins": sum(1 for g in games if g.winner == "A"),
                   "stats_url": f"/api/v1/games?suite=server&map={stem}"}
            if text:
                row["text"] = m.get("mapText")
            out.append(row)
        return {"maps": out, "stale": stale}

    @r.get("/queue")
    @guarded
    def queue():
        d, stale = br().client.queue()
        return {**(d if isinstance(d, dict) else {"queue": d}), "stale": stale}

    # ------------------------------------------------------------------ tournaments
    @r.get("/tournaments")
    @guarded
    def tournaments():
        rows, stale = br().client.tournaments()
        return {"tournaments": [{"id": t.get("id"), "name": t.get("name"), "kind": t.get("kind"),
                                 "status": t.get("status"), "date": t.get("date"),
                                 "bracket_size": t.get("bracketSize"),
                                 "url": f"/api/v1/server/tournaments/{t.get('id')}"}
                                for t in rows if isinstance(rows, list)], "stale": stale}

    @r.get("/tournaments/{tid}")
    @guarded
    def tournament(tid: int, all: bool = False, team: str | None = None, last_rounds: int = 4):
        b = br()
        t, stale = b.client.tournament(tid)
        our_id, our_name = b.our_team()
        focus = b.resolve_team(team)["id"] if team else our_id
        site = b.web.bracket(tid, t.get("status") == "done", (t.get("name"), t.get("kind")))
        games_by_battle = {}
        for rnd in (site or {}).get("rounds") or []:
            for sm in rnd or []:
                if sm and sm.get("battleId"):
                    games_by_battle[sm["battleId"]] = [{"map": g.get("map"), "winner": g.get("winner"),
                                                        "game": g.get("id")} for g in sm.get("games") or []]
        matches = t.get("matches") or []
        nrounds = 1 + max((x.get("bracketMatch", {}).get("round", 0) for x in matches), default=-1)
        rounds: dict[int, list] = {}
        for x in matches:
            bm = x.get("bracketMatch") or {}
            ours = focus in (bm.get("teamAId"), bm.get("teamBId"))
            rd = bm.get("round", 0)
            if not (all or ours or rd >= nrounds - last_rounds):
                continue
            if not bm.get("teamBId") and not bm.get("teamAId"):
                continue
            rounds.setdefault(rd, []).append({
                "position": bm.get("position"), "battle": x.get("battleId"),
                "a": {"team_id": bm.get("teamAId"), "team": x.get("teamAName"), "seed": bm.get("teamASeed"),
                      "score": bm.get("teamAScore"), "elo": x.get("teamAElo")},
                "b": {"team_id": bm.get("teamBId"), "team": x.get("teamBName"), "seed": bm.get("teamBSeed"),
                      "score": bm.get("teamBScore"), "elo": x.get("teamBElo")},
                "winner": bm.get("winner"), "bye": not (bm.get("teamAId") and bm.get("teamBId")),
                "focus": ours, "games": games_by_battle.get(x.get("battleId")),
                "battle_url": f"/api/v1/server/battles/{x['battleId']}" if x.get("battleId") else None})
        return {"id": t.get("id"), "name": t.get("name"), "kind": t.get("kind"), "status": t.get("status"),
                "date": t.get("date"), "bracket_size": t.get("bracketSize"), "rounds_total": nrounds,
                "focus_team_id": focus, "us": {"team_id": our_id, "team": our_name},
                "per_game_maps": site is not None,
                "rounds": [{"round": rd, "name": _round_name(nrounds, rd), "matches": ms}
                           for rd, ms in sorted(rounds.items())], "stale": stale}

    # ------------------------------------------------------------------ auto-submit control
    @r.post("/versions/{ver}/link")
    @guarded
    def link(ver: str, request: Request, body: dict = Body(...)):
        """Link a server version of ours to the arena submission it is: {"arena": "<id>"}."""
        if not isinstance(body, dict) or not isinstance(body.get("arena"), str):
            raise HTTPException(400, 'send JSON: {"arena": "<submission id>"}')
        return br().link(ver, body["arena"], _caller(request))

    @r.post("/pause")
    def pause(request: Request, body: dict | None = Body(None)):
        br().pause(_caller(request), (body or {}).get("reason"))
        return br().summary()["auto_submit"]

    @r.post("/resume")
    def resume(request: Request):
        br().resume(_caller(request))
        return br().summary()["auto_submit"]

    @r.get("/log")
    def server_log(action: str | None = None, limit: int = Query(50, le=500)):
        return br().history({action} if action else None, limit)

    @r.get("/gate")
    def gate():
        b = br()
        g = b.gate()
        g["blocked"] = b.blocked()
        return g

    return r


def _round_name(n: int, rd: int) -> str:
    left = n - rd
    return {1: "final", 2: "semifinal", 3: "quarterfinal"}.get(left, f"round of {2 ** left}")


def _battle_row(x: dict, imported: dict | None) -> dict:
    """A battle from GET /battles (or a team's recent list), with the per-game detail we
    imported, if any."""
    row = {"battle": x.get("id"), "at": x.get("at"), "kind": "ranked" if x.get("ranked") else "unranked",
           "opponent": x.get("opponent"), "opponent_id": x.get("opponentId"),
           "opponent_elo": x.get("opponentElo"), "result": x.get("outcome"),
           "wins": x.get("wins"), "losses": x.get("losses"), "elo_change": x.get("eloChange"),
           "results": "".join({"win": "W", "loss": "L", "draw": "D"}.get(r, ".") for r in x.get("results") or []),
           "url": f"/api/v1/server/battles/{x.get('id')}"}
    if imported:
        row["games"] = [{k: g.get(k) for k in ("game", "map", "result", "arena_game", "watch_url", "replay_url")}
                        for g in imported.get("games") or []]
    return row
