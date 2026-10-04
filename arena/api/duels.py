"""/api/v1/ephemeral and /api/v1/duels (docs/DUELS.md): ad-hoc A-vs-B tests on fresh seeds that
never touch the ladder. Served by the engine (the website forwards every call there)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from arena import duels as D
from arena.state import Arena

FINAL = {"done", "cancelled", "failed"}


def make_router(arena: Arena) -> APIRouter:
    from arena.api.http import caller
    r = APIRouter()

    def book() -> D.Book:
        return D.get(arena)

    def wake():
        d = getattr(arena, "daemon", None)
        if d is not None:
            d.wake.set()

    def eph_json(e) -> dict:
        d = asdict(e)
        d.pop("check_log", None)
        d["url"] = f"/api/v1/ephemeral/{e.id}"
        return d

    # ------------------------------------------------------------------ ephemeral bots
    @r.post("/api/v1/ephemeral", status_code=202)
    async def post_ephemeral(request: Request, file: UploadFile = File(...), name: str = Form(""),
                             ttl_hours: float = Form(D.TTL_HOURS), agent: str | None = Form(None),
                             owner: str | None = Form(None)):
        """An ephemeral bot: checked and compiled like a submission, usable only in duels,
        removed after ttl_hours. The same zip from the same owner renews the existing one."""
        c = caller(request, agent, owner)
        from arena import submit
        try:
            e, new = book().add_ephemeral(await file.read(), name, c.owner, ttl_hours, c.agent)
        except submit.Rejected as ex:
            raise HTTPException(400, str(ex))
        wake()
        return {**eph_json(e), "duplicate": not new}

    @r.get("/api/v1/ephemeral")
    def list_ephemeral(owner: str | None = None):
        return [eph_json(e) for e in sorted(book().ephemeral.values(), key=lambda e: e.created, reverse=True)
                if not owner or e.owner == owner]

    @r.get("/api/v1/ephemeral/{eid}")
    async def get_ephemeral(eid: str, wait: float = Query(0, ge=0, le=1800)):
        e = book().ephemeral.get(eid)
        if e is None:
            raise HTTPException(404, f"no ephemeral bot {eid!r}")
        deadline = asyncio.get_event_loop().time() + wait
        while wait and e.status == "checking" and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
        return {**eph_json(e), "check_log": e.check_log}

    @r.delete("/api/v1/ephemeral/{eid}")
    def delete_ephemeral(eid: str, request: Request):
        e = book().ephemeral.get(eid)
        if e is None:
            raise HTTPException(404, f"no ephemeral bot {eid!r}")
        by = caller(request).owner
        if by not in (e.owner, "local"):
            raise HTTPException(403, f"only {e.owner} can remove {eid}")
        book().remove_ephemeral(eid)
        return {"removed": eid}

    # ------------------------------------------------------------------ duels
    @r.post("/api/v1/duels", status_code=202)
    async def post_duel(request: Request):
        """JSON {a, b, suites?, maps?, seeds? ("random:N" or a list), games?, both_seats?, note?,
        agent?, owner?}. a and b are any submission or ephemeral id."""
        try:
            d = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON")
        if not isinstance(d, dict) or not d.get("a") or not d.get("b"):
            raise HTTPException(400, "JSON with at least 'a' and 'b'")
        c = caller(request, d.get("agent"), d.get("owner"))
        try:
            u = await asyncio.to_thread(
                book().create, owner=c.owner, a=str(d["a"]), b=str(d["b"]), suites=d.get("suites"),
                maps=d.get("maps"), seeds=d.get("seeds") or "random:4", games=d.get("games"),
                both_seats=bool(d.get("both_seats", True)), agent=c.agent, note=str(d.get("note") or ""))
        except (D.DuelError, ValueError) as ex:
            raise HTTPException(400, str(ex))
        q = getattr(arena, "queue", None)
        if q is not None:
            q._bump()
        wake()
        return {"id": u.id, "status": u.status, "games_planned": u.games_planned, "url": f"/api/v1/duels/{u.id}"}

    @r.get("/api/v1/duels")
    def list_duels(owner: str | None = None, limit: int = 50):
        b = book()
        out = sorted((u for u in b.duels.values() if not owner or u.owner == owner),
                     key=lambda u: u.created, reverse=True)[:limit]
        return [{"id": u.id, "owner": u.owner, "a": u.a, "b": u.b, "status": u.status, "created": u.created,
                 "games_planned": u.games_planned, "games_left": b.left(u.id),
                 "games_played": len(b.done.get(u.id, ())), "note": u.note,
                 "url": f"/api/v1/duels/{u.id}"} for u in out]

    @r.get("/api/v1/duels/{did}")
    async def get_duel(did: str, wait: float = Query(0, ge=0, le=1800), games: bool = True):
        """The duel from A's side. wait=S holds the answer until the duel ends (or S seconds)."""
        b = book()
        if did not in b.duels:
            raise HTTPException(404, f"no duel {did!r}")
        deadline = asyncio.get_event_loop().time() + wait
        while wait and b.duels[did].status not in FINAL and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
        return await asyncio.to_thread(b.summary, did, games)

    @r.delete("/api/v1/duels/{did}")
    def cancel_duel(did: str, request: Request):
        b = book()
        u = b.duels.get(did)
        if u is None:
            raise HTTPException(404, f"no duel {did!r}")
        by = caller(request).owner
        if by not in (u.owner, "local"):
            raise HTTPException(403, f"only {u.owner} can cancel {did}")
        b.cancel(did, f"cancelled by {by}")
        q = getattr(arena, "queue", None)
        if q is not None:
            q._bump()
        return {"id": did, "status": b.duels[did].status}

    def game_file(did: str, n: int, kind: str):
        b = book()
        if did not in b.duels:
            raise HTTPException(404, f"no duel {did!r}")
        p = b.dir / did / {"replay": "replays", "metrics": "metrics"}[kind] / f"{n}.{'replay' if kind == 'replay' else 'json'}"
        if not p.exists():
            raise HTTPException(404, f"no {kind} for game {n} (not played, or not kept)")
        return p

    @r.get("/api/v1/duels/{did}/games/{n}/replay")
    def duel_replay(did: str, n: int):
        p = game_file(did, n, "replay")
        from arena.retention import is_gzip
        headers = {"Content-Encoding": "gzip"} if is_gzip(p) else {}
        return FileResponse(p, media_type="application/octet-stream", headers=headers, filename=f"{did}-{n}.replay")

    @r.get("/api/v1/duels/{did}/games/{n}/metrics")
    def duel_metrics(did: str, n: int):
        return FileResponse(game_file(did, n, "metrics"), media_type="application/json")

    @r.get("/duels/{did}/watch/{n}", response_class=HTMLResponse)
    def watch(did: str, n: int):
        from arena.web import viewer
        return viewer.page(f"/api/v1/duels/{did}/games/{n}/replay", f"{did} game {n}")

    return r
