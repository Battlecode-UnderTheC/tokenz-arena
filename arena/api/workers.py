"""/api/v1/workers: the remote worker protocol (docs/WORKERS.md).

In the engine these routes lease, renew and record. In the website the POSTs are forwarded to
the engine like every write, and the downloads (bot zips, maps) are served from disk.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from arena import scheduler
from arena.state import Arena


def token_name(arena: Arena, request: Request) -> str:
    """The calling worker's token name (arena.toml's tokens or the scaler's), else 401."""
    from arena import workers as wk
    bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    name = wk.token_name(arena.cfg, bearer)
    if name is None:
        raise HTTPException(401, "a valid worker token is required (Authorization: Bearer ...)")
    return name


def make_router(arena: Arena) -> APIRouter:
    from arena.api.http import caller
    from arena import workers as wk
    r = APIRouter(prefix="/api/v1/workers")

    def reg() -> "wk.Registry":
        g = getattr(arena, "workers", None)
        if g is None:
            raise HTTPException(503, "remote workers aren't enabled on this arena ([workers] tokens)")
        return g

    def call(fn, *a):
        try:
            return fn(*a)
        except wk.Refused as e:
            raise HTTPException(e.status, str(e))

    async def body(request: Request) -> dict:
        try:
            d = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON")
        if not isinstance(d, dict) or not d.get("worker"):
            raise HTTPException(400, "JSON with at least 'worker', 'version' and 'slots'")
        return d

    @r.get("")
    def list_workers():
        g = getattr(arena, "workers", None)
        if g is not None:
            return {"local_slots": getattr(getattr(arena, "daemon", None), "workers", 0),
                    "remote": g.snapshot()}
        hb = getattr(arena, "heartbeat", None) or {}
        return {"local_slots": hb.get("local_workers", hb.get("workers", 0)), "remote": hb.get("remote", [])}

    @r.post("/lease")
    async def lease(request: Request):
        tok = token_name(arena, request)
        d = await body(request)
        host = caller(request).owner
        return call(reg().lease, str(d["worker"])[:64], host, str(d.get("version")),
                    int(d.get("slots", 0)), tok, max(0, min(int(d.get("want", d.get("slots", 0))), 256)))

    @r.post("/heartbeat")
    async def heartbeat(request: Request):
        tok = token_name(arena, request)
        d = await body(request)
        return call(reg().heartbeat, str(d["worker"])[:64], caller(request).owner, str(d.get("version")),
                    int(d.get("slots", 0)), tok, [str(x) for x in d.get("leases") or []])

    @r.post("/result")
    async def result(request: Request, meta: str = Form(...), replay: UploadFile | None = File(None)):
        tok = token_name(arena, request)
        try:
            m = json.loads(meta)
        except ValueError:
            raise HTTPException(400, "meta must be JSON")
        if not m.get("lease") or not m.get("worker"):
            raise HTTPException(400, "meta needs 'lease' and 'worker'")
        path = None
        if replay is not None:
            inbox = arena.cfg.work / "incoming"
            inbox.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=inbox, suffix=".replay")
            with open(fd, "wb") as f:
                f.write(await replay.read())
            path = Path(tmp)
        res = {k: m.get(k) for k in ("winner", "rounds", "reason", "seconds", "stats", "error", "metrics",
                                     "judge", "judge_check")}
        if res["winner"] not in ("A", "B", None):
            raise HTTPException(400, "winner must be A, B or null")
        if res["judge"] not in ("official", "cpp", None) or res["judge_check"] not in ("same", "different", None):
            raise HTTPException(400, "judge must be official or cpp, judge_check same or different")
        if res["judge"] is None:
            res.pop("judge")                       # an older worker: the official judge
        out = call(reg().result, str(m["worker"])[:64], tok, str(m["lease"]), res, path)
        if path is not None and path.exists():
            path.unlink()                                   # not recorded (a duplicate)
        return out

    @r.get("/bot/{sid}")
    def bot(sid: str, request: Request):
        token_name(arena, request)
        s = arena.subs.get(sid)
        p = arena.cfg.submissions / (s.id if s else sid) / "source.zip"
        if not s or not p.exists():
            raise HTTPException(404, f"no submission {sid!r}")
        return FileResponse(p, media_type="application/zip", filename=f"{s.id}.zip")

    @r.get("/wasm/{sid}")
    def wasm(sid: str, request: Request):
        """A C/C++ bot built to wasm by the judge's clang (built here once, on first ask), with
        its judge-cache file name in X-Wasm-Name: a worker puts it in its own cache and never
        compiles. 404 for a Python bot."""
        token_name(arena, request)
        from arena import runner
        d = arena.cfg.bots / Path(sid).name
        if not d.is_dir():
            raise HTTPException(404, f"no bot {sid!r}")
        p = runner.build_wasm(d)
        if p is None or not p.exists():
            raise HTTPException(404, "not a compiled (C/C++) bot")
        return FileResponse(p, media_type="application/wasm", headers={"X-Wasm-Name": p.name})

    @r.get("/map/{suite}/{name}")
    def map_file(suite: str, name: str, request: Request):
        token_name(arena, request)
        if suite in ("holdout", "check"):
            raise HTTPException(404, "holdout maps never leave the arena")
        for p in scheduler.maps(arena, suite):
            if p.stem == name:
                return FileResponse(p, media_type="text/plain", filename=p.name)
        raise HTTPException(404, f"no map {suite}/{name}")

    return r
