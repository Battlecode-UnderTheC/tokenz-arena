"""MCP tools over streamable HTTP at /mcp, plus GET /llms.txt (DESIGN.md section 6).

Every tool is a thin wrapper that calls the arena's own HTTP API in-process (httpx's ASGI
transport straight into the FastAPI app), so MCP and HTTP can never disagree.

Identity: the HTTP API knows callers by Tailscale (arena/who.py), from the request's client
address and, behind `tailscale serve`, its Tailscale-User-* headers. The in-process call
carries the MCP request's own client address (ASGITransport(client=...)) and forwards its
Tailscale-User-Login, Tailscale-User-Name and Authorization headers, so who.identify sees
exactly what it would have seen had the agent called the HTTP API directly.

Mounting: `mount(app, arena)` adds the /mcp route to the FastAPI app and wraps the app's
lifespan so the MCP session manager runs for as long as the server does.

(No `from __future__ import annotations` here: MCPServer reads the tool signatures'
annotations at runtime to build schemas and find the Context parameter.)
"""

import base64
import io
import json
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Any

import httpx2 as httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from arena.api import llms

FORWARD_HEADERS = ("tailscale-user-login", "tailscale-user-name", "authorization")
FINAL = {"done", "rated", "rejected", "retired"}
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

INSTRUCTIONS = """\
tokenz-arena: the team's arena for UNSW Battlecode "dragons" bots. Every game is played by
the official judge (`unswbc run --sandbox`). Submissions are immutable experiments: a zip,
the parent it builds on, and a note saying what changed and why.

The loop:
1. leaderboard() -> pick a strong base; ideas(q="<topic>") -> see what has been tried.
2. source(id) -> the base's files; report(id) -> where it loses.
3. Change ONE thing. submit(bot, note=<hypothesis + change>, files=..., parent=<base id>)
   -> a draft (plays its parent and the top 3, both seats, unranked).
4. wait(id) -> blocks until the draft verdict; read verdict.vs_parent.beats_parent.
5. Beats its parent -> promote(id) (joins the leaderboard). Otherwise leave it: the note
   and numbers stay in ideas() for the next agent.
GET /llms.txt on this host explains the whole HTTP API.
"""


# ------------------------------------------------------------------ helpers (pure, tested)
def build_zip(files: dict[str, str]) -> bytes:
    """A deterministic zip of {relative path: text} (fixed timestamps, sorted), so the same
    files always give the same bytes and the arena's duplicate check works."""
    if not files:
        raise ValueError("files is empty")
    buf = io.BytesIO()
    seen = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for raw in sorted(files):
            name = raw.replace("\\", "/").removeprefix("./")
            p = PurePosixPath(name)
            if not name or p.is_absolute() or ".." in p.parts or ":" in name or name.endswith("/"):
                raise ValueError(f"bad path in files: {raw!r} (use relative paths like 'src/main.py')")
            if name in seen:
                raise ValueError(f"duplicate path in files: {name!r}")
            seen.add(name)
            text = files[raw]
            if not isinstance(text, str):
                raise ValueError(f"files[{raw!r}] must be text")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode())
    return buf.getvalue()


def unzip_text(blob: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """({path: text} for the zip's text files, {path: why skipped} for the rest)."""
    files, skipped = {}, {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            if info.is_dir() or "__MACOSX" in info.filename or info.filename.endswith(".DS_Store"):
                continue
            data = z.read(info)
            if b"\x00" in data:
                skipped[info.filename] = f"binary, {len(data)} bytes"
                continue
            try:
                files[info.filename] = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped[info.filename] = f"binary, {len(data)} bytes"
    return files, skipped


def result(data: Any) -> CallToolResult:
    """One text block with the JSON (what most agents read) plus the same as structured
    content. Lists go in one block too, so an empty list still reads as "[]"."""
    text = json.dumps(data, indent=1, ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structured_content=data if isinstance(data, dict) else {"result": data})


# ------------------------------------------------------------------ in-process HTTP calls
class _Api:
    """Calls the arena's HTTP API in-process, as the MCP request's own client."""

    def __init__(self, app: FastAPI):
        self.app = app

    async def call(self, ctx: Context | None, method: str, path: str, *, params: dict | None = None,
                   data: dict | None = None, files: dict | None = None, timeout: float = 60,
                   raw: bool = False, out: bool = True, not_found: str | None = None,
                   json_body: dict | None = None) -> Any:
        headers: dict[str, str] = {}
        client = ("127.0.0.1", 0)
        req = None
        if ctx is not None:
            try:
                req = ctx.request_context.request
            except (ValueError, AttributeError):
                req = None
        if req is not None:
            for h in FORWARD_HEADERS:
                if v := req.headers.get(h):
                    headers[h] = v
            if req.client:
                client = (req.client.host, req.client.port or 0)
        transport = httpx.ASGITransport(app=self.app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://arena",
                                     timeout=timeout) as c:
            r = await c.request(
                method, f"/api/v1{path}", headers=headers,
                params={k: v for k, v in (params or {}).items() if v is not None and v != ""},
                data={k: v for k, v in data.items() if v is not None} if data else None,
                files=files, json=json_body)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            # A 404 from a missing route (FastAPI's plain "Not Found"), not a missing record.
            if r.status_code == 404 and not_found and detail == "Not Found":
                raise ToolError(not_found)
            raise ToolError(f"HTTP {r.status_code} from {method} /api/v1{path}: {detail}")
        if raw:
            return r.content
        return result(r.json()) if out else r.json()


def make_server(app: FastAPI) -> MCPServer:
    api = _Api(app)
    mcp = MCPServer("tokenz-arena", instructions=INSTRUCTIONS, version="0.1")

    # -------------------------------------------------------------- submitting
    @mcp.tool(annotations=WRITE)
    async def submit(ctx: Context, bot: str, note: str, files: dict[str, str] | None = None,
                     zip_base64: str | None = None, parent: str | None = None,
                     source: str | None = None, lane: str = "draft", agent: str | None = None,
                     owner: str | None = None) -> CallToolResult:
        """Submit a bot. Returns the new submission (id like "swarm-7", status "checking").

        Give the bot either as `files` ({relative path: text}, bot.toml at the root, e.g.
        {"bot.toml": "[project]\\nlanguage = \\"py\\"\\n", "main.py": "..."}) or as
        `zip_base64` (a base64 zip, max 4 MB). `bot` is the bot's name (a-z0-9_.-); its
        submissions form a line. `note` is the hypothesis and what changed (markdown); it
        becomes the team's experiment log, so be specific. `parent` is the submission id this
        builds on (anyone's): the draft plays it head-to-head first, and that result decides
        whether to promote. lane "draft" (default) = unranked quick verdict vs parent + top 3;
        "ranked" = straight to placement. `agent` labels which of your agents submitted.
        `owner` only takes effect from tagged devices (no Tailscale user login).

        Identical content returns the existing submission with "duplicate": true. Too many of
        your drafts already waiting -> an HTTP 429 error; wait for some to finish. Bots must
        not read environment variables (rejected). Next: wait(id)."""
        if (files is None) == (zip_base64 is None):
            raise ToolError("pass exactly one of files ({path: text}) or zip_base64")
        try:
            blob = build_zip(files) if files is not None else base64.b64decode(zip_base64, validate=True)
        except (ValueError, TypeError) as e:
            raise ToolError(f"can't build the zip: {e}") from e
        return await api.call(ctx, "POST", "/submissions",
                              data={"bot": bot, "note": note, "parent": parent, "source": source,
                                    "lane": lane, "agent": agent, "owner": owner},
                              files={"file": ("bot.zip", blob, "application/zip")})

    @mcp.tool(annotations=READ)
    async def submission(ctx: Context, id: str) -> CallToolResult:
        """One submission now: status (checking, rejected, queued, playing, done, placing,
        rated, retired), lane, parent, note, the rejection reason and check log if rejected,
        rating, and `verdict` ({complete, vs_parent: {W, D, L, score, beats_parent},
        opponents: [...]}). Doesn't block; use wait(id) to block until it's decided."""
        return await api.call(ctx, "GET", f"/submissions/{id}")

    @mcp.tool(annotations=READ)
    async def wait(ctx: Context, id: str, timeout_s: int = 600) -> CallToolResult:
        """Block until a submission is decided (status done, rated, rejected or retired) or
        `timeout_s` passes (max 1800), then return it like submission(id), plus "finished"
        (false on timeout: just call wait again). A draft usually takes a few minutes. Then
        read verdict.vs_parent.beats_parent: true -> promote(id); false/null -> don't. If
        status is "rejected", read `reason` and `check_log`, fix, and resubmit."""
        timeout_s = max(0, min(int(timeout_s), 1800))
        start = time.monotonic()
        while True:
            left = timeout_s - (time.monotonic() - start)
            chunk = max(0, min(30, int(left)))
            s = await api.call(ctx, "GET", f"/submissions/{id}", params={"wait": chunk},
                               timeout=chunk + 30, out=False)
            if s.get("status") in FINAL or chunk == 0:
                s["finished"] = s.get("status") in FINAL
                return result(s)
            try:   # keeps clients that show progress informed (and their connections busy)
                await ctx.report_progress(time.monotonic() - start, timeout_s,
                                          f"{id}: {s.get('status')}")
            except Exception:
                pass

    @mcp.tool(annotations=WRITE)
    async def promote(ctx: Context, id: str) -> CallToolResult:
        """Turn a draft into a ranked submission: it gets full placement and becomes its bot's
        current entry on the leaderboard. Only promote a draft whose verdict beats its parent
        (verdict.vs_parent.beats_parent == true). Returns the submission. Recorded with who
        promoted it."""
        return await api.call(ctx, "POST", f"/submissions/{id}/promote")

    # -------------------------------------------------------------- reading a submission
    @mcp.tool(annotations=READ)
    async def report(ctx: Context, id: str) -> CallToolResult:
        """A submission's diagnostic report: record and score, by_opponent and by_map (worst
        first), deaths by cause, CPU (p99/max vs the 100M cap), rounds_mean, worst_losses
        (with replay URLs), holdout record, and vs_parent. Use it to find what to fix next."""
        return await api.call(ctx, "GET", f"/submissions/{id}/report")

    @mcp.tool(annotations=READ)
    async def source(ctx: Context, id: str) -> CallToolResult:
        """The submitted bot's files: {"submission", "files": {path: text}, "skipped": {path:
        why}} (binary files are skipped). Use it to build on someone's bot: edit the files and
        pass them to submit(files=..., parent=id)."""
        blob = await api.call(ctx, "GET", f"/submissions/{id}/source", raw=True)
        files, skipped = unzip_text(blob)
        return result({"submission": id, "files": files, "skipped": skipped})

    # -------------------------------------------------------------- ranking, bots, ideas
    @mcp.tool(annotations=READ)
    async def leaderboard(ctx: Context) -> CallToolResult:
        """Current ranked submissions, best first: rank, submission, bot, owner, status, elo,
        err (the ± of the rating), games, record {W, D, L}, score (win %), holdout, note.
        Start here to pick a strong base."""
        return await api.call(ctx, "GET", "/leaderboard")

    @mcp.tool(annotations=READ)
    async def bot(ctx: Context, name: str) -> CallToolResult:
        """A bot's line: owner, current submission, every submission (oldest first, with
        notes and verdicts), lineage (id -> parent) and the current one's head-to-head table."""
        return await api.call(ctx, "GET", f"/bots/{name}")

    @mcp.tool(annotations=READ)
    async def ideas(ctx: Context, q: str = "", bot: str | None = None, outcome: str | None = None,
                    limit: int = 50) -> CallToolResult:
        """The experiment log: submissions' notes paired with what happened, newest first.
        `q` = words that must all appear in the note/bot/id; `outcome` = beat_parent,
        lost_to_parent, tied_parent, no_parent, pending, rejected or promoted. Each row: id,
        bot, owner, agent, parent, note, status, promoted, outcome, reason, verdict. Check this
        before starting an idea: it may already have been tried."""
        return await api.call(ctx, "GET", "/ideas",
                              params={"q": q, "bot": bot, "outcome": outcome, "limit": limit})

    # -------------------------------------------------------------- games and maps
    @mcp.tool(annotations=READ)
    async def games(ctx: Context, sub: str | None = None, vs: str | None = None,
                    map: str | None = None, suite: str | None = None, lane: str | None = None,
                    result: str | None = None, limit: int = 50) -> CallToolResult:
        """Games, newest first, with verdicts and a stats summary. Filters: sub (a submission
        id in either seat), vs (another id in either seat), map, suite (official, rotating,
        holdout, server, check), lane (draft, placement, holdout, continuous, check, server),
        result (W, D or L from `sub`'s side; needs sub), limit (max 1000). Each row: id, a, b,
        winner (A/B/null), rounds, reason, map, seed, summary, replay_url, watch_url."""
        return await api.call(ctx, "GET", "/games",
                              params={"sub": sub, "vs": vs, "map": map, "suite": suite,
                                      "lane": lane, "result": result, "limit": limit})

    @mcp.tool(annotations=READ)
    async def game(ctx: Context, id: int) -> CallToolResult:
        """One game in full: seats, map, seed, winner, rounds, end reason, error, and stats
        (per team: deaths by cause, CPU points, dragon counts/lengths every 10 rounds;
        tiebreak numbers). replay_url downloads the .replay; watch_url opens the viewer."""
        return await api.call(ctx, "GET", f"/games/{id}")

    @mcp.tool(annotations=READ)
    async def map_stats(ctx: Context) -> CallToolResult:
        """Per map: games, seat_a_wins, seat_b_wins, draws, how games end, rounds_mean."""
        return await api.call(ctx, "GET", "/stats/maps")

    @mcp.tool()
    async def edit_note(ctx: Context, id: str, note: str) -> CallToolResult:
        """Replace a submission's note (only its owner can). Use it to record what the verdict
        showed, or correct the hypothesis; earlier versions are kept in note_history."""
        return await api.call(ctx, "PATCH", f"/submissions/{id}", json_body={"note": note})

    @mcp.tool(annotations=READ)
    async def lineage(ctx: Context, bot: str | None = None, root: str | None = None) -> CallToolResult:
        """The family forest: each submission under its parent (across bots), with its verdict
        against its parent, status, rating and whether it's its bot's current version. Use it to
        find the lines that keep improving, before choosing what to build on."""
        return await api.call(ctx, "GET", "/lineage", params={"bot": bot, "root": root,
                                                              "hide_rejected": "true"})

    @mcp.tool(annotations=READ)
    async def maps(ctx: Context) -> CallToolResult:
        """Every public map by suite (official, tournament, server, custom = team uploads,
        rotating = this week's generated maps), with a preview link. Hidden uploads are only
        counted."""
        return await api.call(ctx, "GET", "/maps")

    @mcp.tool()
    async def upload_map(ctx: Context, text: str, name: str, note: str = "",
                         hidden: bool = False) -> CallToolResult:
        """Add a map every bot in the arena plays on (the `custom` suite), so bots can't overfit
        the official maps. `text` is the .map file's contents (first line `MAP <w> <h>`). The
        official judge plays a test game on it first and refuses maps it can't load. With
        hidden=true it goes to the holdout instead: only the top 4 play it and nobody sees it."""
        return await api.call(ctx, "POST", "/maps", timeout=660,
                              data={"name": name, "note": note, "hidden": "true" if hidden else None},
                              files={"file": (f"{name}.map", text.encode(), "text/plain")})

    @mcp.tool(annotations=READ)
    async def queue(ctx: Context) -> CallToolResult:
        """What the judge is doing: workers, running games, submissions being checked, and
        waiting submissions with games_left. Use it to estimate how long wait() will take."""
        return await api.call(ctx, "GET", "/queue")

    # -------------------------------------------------------------- the competition server
    nobridge = ("server bridge not available: this arena doesn't serve /api/v1/server{} yet "
                "(DESIGN.md section 8)")

    @mcp.tool(annotations=READ)
    async def server_status(ctx: Context) -> CallToolResult:
        """The competition server, as the arena sees it: our live bot (server version and
        arena id), rank, rating, record, quota left, auto-submit state and candidate."""
        return await api.call(ctx, "GET", "/server", not_found=nobridge.format(""))

    @mcp.tool(annotations=READ)
    async def server_battles(ctx: Context, limit: int = 20) -> CallToolResult:
        """Our recent battles on the competition server: opponent, kind, our side, W-L, rating
        change, per-game map and result (with arena game ids for replays)."""
        return await api.call(ctx, "GET", "/server/battles", params={"limit": limit},
                              not_found=nobridge.format("/battles"))

    @mcp.tool(annotations=READ)
    async def server_battle(ctx: Context, id: str) -> CallToolResult:
        """One competition-server battle: each game's map, seats, winner, end reason, the crash
        or timeout log, and the arena game id (use game(id) for stats and the replay)."""
        return await api.call(ctx, "GET", f"/server/battles/{id}",
                              not_found=nobridge.format("/battles/{id}"))

    @mcp.tool(annotations=READ)
    async def server_leaderboard(ctx: Context) -> CallToolResult:
        """The competition server's ladder, with our row marked."""
        return await api.call(ctx, "GET", "/server/leaderboard",
                              not_found=nobridge.format("/leaderboard"))

    @mcp.tool(annotations=READ)
    async def server_quota(ctx: Context) -> CallToolResult:
        """Competition-server games left this hour, when more free up, and recent challenges
        with who started them."""
        return await api.call(ctx, "GET", "/server/quota", not_found=nobridge.format("/quota"))

    return mcp


TOOLS = ("submit", "submission", "wait", "promote", "report", "source", "leaderboard", "bot",
         "ideas", "games", "game", "map_stats", "maps", "upload_map", "edit_note", "lineage",
         "queue", "server_status",
         "server_battles",
         "server_battle", "server_leaderboard", "server_quota")


def mount(app: FastAPI, arena) -> None:
    """Adds /mcp (streamable HTTP) and GET /llms.txt to the arena's FastAPI app."""
    server = make_server(app)
    # The arena is tailnet-only and reached by several host names (tailscale serve's ts.net
    # name, the tailnet IP, localhost), so the SDK's localhost-only Host check is off.
    sub = server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    app.router.routes.extend(sub.routes)

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with server.session_manager.run():
            async with inner(a) as state:
                yield state

    app.router.lifespan_context = lifespan
    app.state.mcp = server

    @app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
    def llms_txt(request: Request):
        return PlainTextResponse(llms.render(arena, base_url(request)))


def base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "arena"
    return f"{proto}://{host}"
