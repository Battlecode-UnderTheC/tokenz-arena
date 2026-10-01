"""The HTTP API (DESIGN.md section 6), served by `arena serve` in the same process as the
judge workers, so it reads their in-memory state directly. Writes: submitting, promoting,
and pausing/resuming the server bridge.

Other routers mount here too: the website (arena/web/pages.py), MCP (arena/api/mcp.py) and
the server endpoints (arena/server/routes.py), when present.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from arena import report, scheduler, submit, who
from arena.state import DONE, RATED, REJECTED, Arena, result_for, wdl
from arena.web import viewer

FINAL = {DONE, RATED, REJECTED, "retired"}


def caller(request: Request, agent: str | None = None, owner: str | None = None) -> who.Caller:
    return who.identify(request.headers, request.client.host if request.client else None,
                        agent, owner)


def sub_json(arena: Arena, s, detail: bool = False) -> dict:
    d = asdict(s)
    d.pop("check_log", None) if not detail else None
    d["url"] = f"/api/v1/submissions/{s.id}"
    r = arena.ratings.get(s.id) if hasattr(arena, "ratings") else None
    d["rating"] = r
    if s.status not in ("checking", "rejected", "external"):
        d["verdict"] = report.verdict(arena, s.id)
    return d


def game_json(g, full: bool = False) -> dict:
    d = asdict(g)
    if not full:
        st = d.pop("stats", {}) or {}
        d["summary"] = {k: {"died": t.get("died"), "dragons_used": t.get("dragons_used"),
                            "points_max": t.get("points_max")}
                        for k, t in (st.get("teams") or {}).items()}
        if st.get("tiebreak"):
            d["summary"]["tiebreak"] = st["tiebreak"]
    d["replay_url"] = f"/api/v1/games/{g.id}/replay" if g.replay else None
    d["watch_url"] = f"/games/{g.id}" if g.replay else None
    return d


def leaderboard(arena: Arena) -> list[dict]:
    rows = []
    ratings = getattr(arena, "ratings", {}) or {}
    for rank, sid in enumerate(scheduler.ranking(arena), 1):
        s = arena.subs[sid]
        games = [g for g in arena.games_of(sid)
                 if g.suite in ("official", "rotating") and g.a != g.b]
        r = wdl(games, sid)
        n = sum(r.values())
        hold = [g for g in arena.games_of(sid) if g.suite == "holdout"]
        rows.append({"rank": rank, "submission": sid, "bot": s.bot, "owner": s.owner,
                     "status": s.status, **(ratings.get(sid) or {"elo": None, "err": None}),
                     "games": n, "record": r,
                     "score": round(100 * (r["W"] + r["D"] / 2) / n, 1) if n else None,
                     "holdout": wdl(hold, sid) if hold else None, "note": s.note[:200]})
    return rows


def bot_detail(arena: Arena, bot: str) -> dict | None:
    """GET /api/v1/bots/{bot}: a bot's line, lineage and head-to-head (None if no such bot)."""
    b = arena.bots.get(bot)
    if not b:
        return None
    subs = sorted((s for s in arena.subs.values() if s.bot == bot), key=lambda s: s.created)
    cur = b.get("current")
    h2h = []
    if cur:
        games = [g for g in arena.games_of(cur) if g.a != g.b and g.suite in ("official", "rotating")]
        opps = {g.b if g.a == cur else g.a for g in games}
        for o in sorted(opps):
            r = wdl([g for g in games if o in (g.a, g.b)], cur)
            h2h.append({"opponent": o, **r})
    return {"bot": bot, **b, "submissions": [sub_json(arena, s) for s in subs],
            "lineage": [{"id": s.id, "parent": s.parent} for s in subs], "head_to_head": h2h}


def ideas(arena: Arena, q: str = "", bot: str | None = None, outcome: str | None = None,
          limit: int = 50) -> list[dict]:
    """GET /api/v1/ideas: notes paired with their outcomes, newest first."""
    terms = [t.lower() for t in q.split()]
    out = []
    for s in sorted(arena.subs.values(), key=lambda s: s.created, reverse=True):
        if s.status == "external" or (bot and s.bot != bot):
            continue
        text = f"{s.note} {s.bot} {s.id}".lower()
        if terms and not all(t in text for t in terms):
            continue
        v = report.verdict(arena, s.id) if s.status not in ("checking", "rejected") else {}
        beats = (v.get("vs_parent") or {}).get("beats_parent")
        promoted = s.lane == "ranked"
        o = ("rejected" if s.status == REJECTED else "pending" if not v.get("complete")
             else "no_parent" if not s.parent else "beat_parent" if beats
             else "lost_to_parent" if beats is False else "tied_parent")
        if outcome and outcome not in (o, "promoted" if promoted else "-"):
            continue
        out.append({"id": s.id, "bot": s.bot, "owner": s.owner, "agent": s.agent,
                    "parent": s.parent, "note": s.note, "source": s.source,
                    "created": s.created, "status": s.status, "promoted": promoted,
                    "outcome": o, "reason": s.reason, "verdict": v})
        if len(out) >= limit:
            break
    return out


def find_games(arena: Arena, sub: str | None = None, vs: str | None = None,
               map: str | None = None, suite: str | None = None, lane: str | None = None,
               result: str | None = None, limit: int = 50) -> list:
    """GET /api/v1/games: matching Game objects, newest first."""
    out = []
    for g in reversed(arena.games):
        if sub and sub not in (g.a, g.b):
            continue
        if vs and vs not in (g.a, g.b):
            continue
        if (map and g.map != map) or (suite and g.suite != suite) or (lane and g.lane != lane):
            continue
        if result and sub and (result_for(g, sub) or "E") != result.upper()[:1]:
            continue
        out.append(g)
        if len(out) >= limit:
            break
    return out


def find_game(arena: Arena, gid: int):
    for g in reversed(arena.games):
        if g.id == gid:
            return g
    return None


def map_stats(arena: Arena) -> list[dict]:
    """GET /api/v1/stats/maps: per map, games, how they end, first-seat advantage."""
    rows: dict[str, dict] = {}
    for g in arena.games:
        if g.error or g.suite in ("check", "holdout"):
            continue
        r = rows.setdefault(f"{g.suite}/{g.map}", {"map": g.map, "suite": g.suite, "games": 0,
                                                   "seat_a_wins": 0, "seat_b_wins": 0, "draws": 0,
                                                   "ends": {}, "rounds": 0})
        r["games"] += 1
        r["rounds"] += g.rounds or 0
        key = {"A": "seat_a_wins", "B": "seat_b_wins"}.get(g.winner or "", "draws")
        r[key] += 1
        end = (g.reason or "?").split(",")[0]
        r["ends"][end] = r["ends"].get(end, 0) + 1
    for r in rows.values():
        r["rounds_mean"] = round(r.pop("rounds") / r["games"], 1)
    return sorted(rows.values(), key=lambda r: (r["suite"], r["map"]))


def queue(arena: Arena) -> dict:
    """GET /api/v1/queue: what's playing and what's waiting."""
    with arena.lock:
        running = [{"a": r["job"].a, "b": r["job"].b, "map": r["job"].map.rsplit("/", 1)[-1][:-4],
                    "seed": r["job"].seed, "lane": r["job"].lane, "owner": r["job"].owner,
                    "started": r["started"]} for r in arena.running.values()]
        checking = [s.id for s in arena.subs.values() if s.status == "checking"]
    waiting: dict[str, dict] = {}
    for s in list(arena.subs.values()):
        if s.status in ("queued", "playing", "placing"):
            owed = scheduler.owed_for(arena, s)
            taken = {(g.a, g.b, g.suite, g.map, g.seed) for g in arena.games_of(s.id)}
            left = sum(1 for j in owed if j.key not in taken)
            if left:
                waiting[s.id] = {"owner": s.owner, "lane": s.lane, "games_left": left,
                                 "games_total": len(owed)}
    return {"workers": getattr(getattr(arena, "daemon", None), "workers", 0),
            "running": running, "checking": checking, "waiting": waiting}


def create_app(arena: Arena) -> FastAPI:
    app = FastAPI(title="tokenz-arena", version="0.1", docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    api = APIRouter(prefix="/api/v1")

    def get_sub(sid: str):
        s = arena.subs.get(sid)
        if not s:
            raise HTTPException(404, f"no submission {sid!r}")
        return s

    # ------------------------------------------------------------ submissions
    @api.post("/submissions", status_code=202)
    async def post_submission(request: Request, file: UploadFile = File(...), bot: str = Form(...),
                              note: str = Form(""), parent: str | None = Form(None),
                              source: str | None = Form(None), lane: str = Form("draft"),
                              agent: str | None = Form(None), owner: str | None = Form(None)):
        c = caller(request, agent, owner)
        blob = await file.read()
        cap = arena.cfg.get("submit", "max_queued_drafts", 8)
        with arena.lock:
            queued = sum(1 for s in arena.subs.values() if s.owner == c.owner and s.lane == "draft"
                         and s.status in ("checking", "queued", "playing"))
        if lane == "draft" and queued >= cap:
            raise HTTPException(429, f"{c.owner} already has {queued} drafts waiting (limit {cap}); "
                                     "wait for some to finish")
        try:
            s, new = submit.accept(arena, blob=blob, bot=bot, owner=c.owner, note=note,
                                   parent=parent or None, source=source or None, lane=lane,
                                   agent=c.agent)
        except submit.Rejected as e:
            raise HTTPException(400, str(e))
        if getattr(arena, "daemon", None):
            arena.daemon.wake.set()
        body = sub_json(arena, s)
        body["duplicate"] = not new
        return JSONResponse(body, status_code=202 if new else 200)

    @api.get("/submissions")
    def list_submissions(bot: str | None = None, owner: str | None = None, lane: str | None = None,
                         status: str | None = None, agent: str | None = None, limit: int = 50):
        out = [s for s in arena.subs.values() if s.status != "external"
               and (not bot or s.bot == bot) and (not owner or s.owner == owner)
               and (not lane or s.lane == lane) and (not status or s.status == status)
               and (not agent or s.agent == agent)]
        out.sort(key=lambda s: s.created, reverse=True)
        return [sub_json(arena, s) for s in out[:limit]]

    @api.get("/submissions/{sid}")
    async def get_submission(sid: str, wait: float = Query(0, ge=0, le=1800)):
        s = get_sub(sid)
        deadline = asyncio.get_event_loop().time() + wait
        while wait and s.status not in FINAL and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
        return sub_json(arena, s, detail=True)

    @api.post("/submissions/{sid}/promote")
    def promote(sid: str, request: Request):
        s = get_sub(sid)
        if s.status in ("checking", REJECTED, "external"):
            raise HTTPException(409, f"{sid} is {s.status}; only checked submissions can be promoted")
        if s.lane == "ranked" and s.status in ("placing", RATED):
            return sub_json(arena, s)
        submit.make_current(arena, s, by=caller(request).owner)
        arena.daemon.wake.set() if getattr(arena, "daemon", None) else None
        return sub_json(arena, s)

    @api.get("/submissions/{sid}/report")
    def get_report(sid: str):
        get_sub(sid)
        return report.build(arena, sid)

    @api.get("/submissions/{sid}/source")
    def get_source(sid: str):
        get_sub(sid)
        p = arena.cfg.submissions / sid / "source.zip"
        if not p.exists():
            raise HTTPException(404, "no source")
        return FileResponse(p, media_type="application/zip", filename=f"{sid}.zip")

    # ------------------------------------------------------------ ranking and bots
    @api.get("/leaderboard")
    def get_leaderboard():
        return leaderboard(arena)

    @api.get("/bots")
    def get_bots():
        return [{"bot": name, **b, "submissions": sum(1 for s in arena.subs.values() if s.bot == name)}
                for name, b in sorted(arena.bots.items())]

    @api.get("/bots/{bot}")
    def get_bot(bot: str):
        d = bot_detail(arena, bot)
        if d is None:
            raise HTTPException(404, f"no bot {bot!r}")
        return d

    @api.get("/ideas")
    def get_ideas(q: str = "", bot: str | None = None, outcome: str | None = None,
                  limit: int = 50):
        return ideas(arena, q, bot, outcome, limit)

    # ------------------------------------------------------------ games
    @api.get("/games")
    def get_games(sub: str | None = None, vs: str | None = None, map: str | None = None,
                  suite: str | None = None, lane: str | None = None, result: str | None = None,
                  limit: int = Query(50, le=1000)):
        return [game_json(g) for g in find_games(arena, sub, vs, map, suite, lane, result, limit)]

    def get_game_obj(gid: int):
        g = find_game(arena, gid)
        if g is None:
            raise HTTPException(404, f"no game {gid}")
        return g

    @api.get("/games/{gid}")
    def get_game(gid: int):
        return game_json(get_game_obj(gid), full=True)

    @api.get("/games/{gid}/replay")
    def get_replay(gid: int):
        g = get_game_obj(gid)
        p = arena.cfg.replays / (g.replay or "")
        if not g.replay or not p.exists():
            raise HTTPException(404, "replay not kept")
        return FileResponse(p, media_type="application/octet-stream",
                            filename=f"{g.a}-vs-{g.b}-on-{g.map}-{g.id}.replay")

    @api.get("/stats/maps")
    def get_map_stats():
        return map_stats(arena)

    @api.get("/queue")
    def get_queue():
        return queue(arena)

    app.include_router(api)

    # ------------------------------------------------------------ health and viewer assets
    @app.get("/health")
    def health():
        from importlib.metadata import version
        hb = __import__("arena.store", fromlist=["read_json"]).read_json(
            arena.cfg.data_dir / "heartbeat.json", {})
        du = shutil.disk_usage(arena.cfg.data_dir)
        return {"ok": True, "started": arena.started, "heartbeat": hb,
                "workers": getattr(getattr(arena, "daemon", None), "workers", 0),
                "unswbc": version("unswbc"), "submissions": len(arena.subs),
                "games": len(arena.games), "disk_free_gb": round(du.free / 2**30, 1)}

    assets = viewer.extract(arena.cfg.data_dir / "viewer")

    @app.get("/viewer/{name}")
    def viewer_asset(name: str):
        if name not in ("webview.js", "webview.css"):
            raise HTTPException(404)
        return FileResponse(assets / name,
                            media_type="text/javascript" if name.endswith(".js") else "text/css")

    @app.get("/watch/{gid}", response_class=HTMLResponse)
    def watch(gid: int):
        g = get_game_obj(gid)
        return viewer.page(f"/api/v1/games/{gid}/replay", f"{g.a} vs {g.b} on {g.map}")

    for mod, attr in (("arena.web.pages", "router"), ("arena.server.routes", "router")):
        try:
            m = __import__(mod, fromlist=[attr])
            app.include_router(m.make_router(arena) if hasattr(m, "make_router") else getattr(m, attr))
        except ModuleNotFoundError:
            pass
    try:
        from arena.api import mcp
        if hasattr(mcp, "mount"):
            mcp.mount(app, arena)
    except ModuleNotFoundError:
        pass
    return app
