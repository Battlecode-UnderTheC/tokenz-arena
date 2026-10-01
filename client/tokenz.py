#!/usr/bin/env python3
"""tokenz: the command-line client for tokenz-arena, the team's arena for unswbc bots.

One file, Python 3.9+ standard library only. Get it from the arena itself:

    curl -fsSL https://tokenz-arena.lonk-kelvin.ts.net/tokenz.py -o ~/.local/bin/tokenz
    chmod +x ~/.local/bin/tokenz
    tokenz --help

Common commands:

    tokenz submit ./mybot --bot swarm --note "lunge at enemy heads" --parent swarm-6 --wait
    tokenz show swarm-7                  # status and verdict (vs parent, vs top bots)
    tokenz report swarm-7                # where it loses: maps, opponents, deaths, CPU
    tokenz promote swarm-7               # draft -> ranked (joins the leaderboard)
    tokenz leaderboard
    tokenz ideas lunge                   # what has been tried, with outcomes
    tokenz get swarm-7 ./swarm-7         # download a submission's source to build on

Every command takes --json for machine-readable output (for agents). The arena's address
comes from --arena, then $TOKENZ_ARENA, then ~/.config/tokenz/config.json {"arena": ...},
then the address this file was downloaded from.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path

DEFAULT_ARENA = "https://tokenz-arena.lonk-kelvin.ts.net"   # rewritten by the arena on download
VERSION = "1.0"
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules", "build",
             "cmake-build-debug", "cmake-build-release", ".idea", ".vscode", "replays", ".cache"}
SKIP_SUFFIXES = {".pyc", ".o", ".obj", ".so", ".dylib", ".dll", ".exe", ".replay", ".zip", ".wasm"}
MAX_ZIP = 4 * 1024 * 1024


class Fail(Exception):
    pass


# ---------------------------------------------------------------------------- http

def arena_url(args) -> str:
    if args.arena:
        return args.arena.rstrip("/")
    if os.environ.get("TOKENZ_ARENA"):
        return os.environ["TOKENZ_ARENA"].rstrip("/")
    cfg = Path.home() / ".config" / "tokenz" / "config.json"
    try:
        return json.loads(cfg.read_text())["arena"].rstrip("/")
    except (OSError, ValueError, KeyError):
        return DEFAULT_ARENA


def request(args, method: str, path: str, body: bytes | None = None,
            content_type: str | None = None, timeout: float = 60, raw: bool = False):
    url = arena_url(args) + path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", f"tokenz/{VERSION}")
    if getattr(args, "agent", None):
        req.add_header("Authorization", f"Bearer {args.agent}")   # an agent label, not a secret
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise Fail(f"{method} {path}: HTTP {e.code}: {detail}")
    except (urllib.error.URLError, OSError) as e:
        raise Fail(f"can't reach the arena at {arena_url(args)} ({e}). "
                   "Are you on the tailnet? Is --arena right?")
    if raw:
        return data
    return json.loads(data) if data else None


def get(args, path: str, **params):
    q = {k: v for k, v in params.items() if v not in (None, "", False)}
    return request(args, "GET", path + ("?" + urllib.parse.urlencode(q) if q else ""),
                   timeout=params.get("wait", 0) + 60 if "wait" in params else 60)


def multipart(fields: dict, filename: str, blob: bytes) -> tuple[bytes, str]:
    edge = f"tokenz{uuid.uuid4().hex}"
    out = bytearray()
    for k, v in fields.items():
        if v is None or v == "":
            continue
        out += f'--{edge}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += (f'--{edge}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/zip\r\n\r\n").encode() + blob + b"\r\n"
    out += f"--{edge}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={edge}"


# ---------------------------------------------------------------------------- output

def emit(args, data, human=None):
    if args.json or human is None:
        print(json.dumps(data, indent=2))
    else:
        human(data)


def table(rows: list[list], headers: list[str]):
    rows = [["" if c is None else str(c) for c in r] for r in rows]
    w = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w[i]) for i, h in enumerate(headers)).rstrip())
    for r in rows:
        print("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip())


def wdl(r: dict) -> str:
    return f"{r.get('W', 0)}-{r.get('D', 0)}-{r.get('L', 0)}"


def show_submission(d: dict):
    print(f"{d['id']}  [{d['lane']}, {d['status']}]  owner {d['owner']}"
          + (f" / {d['agent']}" if d.get("agent") else ""))
    if d.get("parent"):
        print(f"  parent   {d['parent']}")
    if d.get("source"):
        print(f"  source   {d['source']}")
    if d.get("rating") and d["rating"].get("elo") is not None:
        r = d["rating"]
        print(f"  elo      {r['elo']} ± {r.get('err') or '?'}  ({r.get('games')} games)")
    if d.get("reason"):
        print(f"  REJECTED {d['reason']}")
    if d.get("note"):
        print("  note     " + d["note"].strip().replace("\n", "\n           "))
    v = d.get("verdict")
    if v:
        state = "complete" if v.get("complete") else "in progress"
        print(f"  verdict  ({state})")
        if v.get("vs_parent"):
            p = v["vs_parent"]
            b = {True: "BEATS parent", False: "loses to parent", None: "even with parent"}[p.get("beats_parent")]
            ci = p.get("ci")
            noise = "" if p.get("significant") or not ci else "  (within noise)"
            print(f"    vs parent {p['parent']}: {wdl(p)}  {p.get('score')}%"
                  + (f" [95% CI {ci[0]}-{ci[1]}]" if ci else "") + f"  -> {b}{noise}")
        for o in v.get("opponents") or []:
            ci = o.get("ci")
            print(f"    vs {o['opponent']}: {wdl(o)}  {o.get('score')}%"
                  + (f" [95% CI {ci[0]}-{ci[1]}]" if ci else ""))
    print(f"  page     {DEFAULT_URL_HINT}/s/{d['id']}")


DEFAULT_URL_HINT = ""


# ---------------------------------------------------------------------------- zip + git

def build_zip(directory: Path) -> tuple[bytes, list[str]]:
    if not (directory / "bot.toml").is_file():
        raise Fail(f"{directory}: no bot.toml (an unswbc bot project has one at its root)")
    buf = io.BytesIO()
    names = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(directory.rglob("*")):
            rel = p.relative_to(directory)
            if any(part in SKIP_DIRS or part.startswith(".") and part != "." for part in rel.parts[:-1]):
                continue
            if not p.is_file() or p.is_symlink() or p.suffix in SKIP_SUFFIXES or p.name.startswith("."):
                continue
            info = zipfile.ZipInfo(rel.as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())       # fixed dates: same files -> same zip
            names.append(rel.as_posix())
    blob = buf.getvalue()
    if len(blob) > MAX_ZIP:
        raise Fail(f"the zip is {len(blob) / 2**20:.1f} MB; the limit is 4 MB")
    return blob, names


def git(directory: Path, *cmd) -> str:
    return subprocess.run(["git", "-C", str(directory), *cmd], capture_output=True, text=True,
                          check=True).stdout.strip()


def source_link(directory: Path) -> tuple[str | None, str | None]:
    """(GitHub link to this directory at HEAD, warning) or (None, None) outside a GitHub repo."""
    try:
        top = Path(git(directory, "rev-parse", "--show-toplevel"))
        sha = git(directory, "rev-parse", "HEAD")
        remote = git(directory, "remote", "get-url", "origin")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None, None
    if "github.com" not in remote:
        return None, None
    slug = remote.split("github.com", 1)[1].lstrip(":/").removesuffix(".git")
    rel = directory.resolve().relative_to(top.resolve()).as_posix()
    link = f"https://github.com/{slug}/tree/{sha}" + (f"/{rel}" if rel != "." else "")
    warn = None
    if git(directory, "status", "--porcelain", "--", ".").strip():
        warn = "the directory has uncommitted changes, so the source link won't match the zip"
    else:
        try:
            if git(directory, "branch", "-r", "--contains", sha) == "":
                warn = "HEAD isn't pushed yet, so the source link won't open until you push"
        except subprocess.CalledProcessError:
            pass
    return link, warn


# ---------------------------------------------------------------------------- commands

def cmd_submit(args):
    d = Path(args.dir)
    blob, names = build_zip(d)
    source, warn = (args.source, None) if args.source else (None, None)
    if not source and not args.no_source:
        source, warn = source_link(d)
    if warn:
        print(f"warning: {warn}", file=sys.stderr)
    note = args.note
    if args.note_file:
        note = Path(args.note_file).read_text()
    if not note:
        raise Fail("--note is required: say what this tries and what changed (it goes in /ideas)")
    body, ctype = multipart({"bot": args.bot, "note": note, "parent": args.parent,
                             "source": source, "lane": args.lane, "agent": args.agent,
                             "owner": args.owner}, f"{args.bot}.zip", blob)
    d = request(args, "POST", "/api/v1/submissions", body, ctype)
    if not args.json:
        dup = " (identical to an existing submission)" if d.get("duplicate") else ""
        print(f"submitted {len(names)} files as {d['id']}{dup}: {arena_url(args)}/s/{d['id']}")
    if args.wait:
        d = wait_for(args, d["id"], args.timeout)
    emit(args, d, show_submission if args.wait else (lambda _: None))
    if args.wait and d.get("status") == "rejected":
        return 1


def wait_for(args, sid: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while True:
        left = max(1, min(300, deadline - time.time()))
        d = get(args, f"/api/v1/submissions/{urllib.parse.quote(sid)}", wait=int(left))
        if d["status"] in ("done", "rated", "rejected", "retired") or time.time() >= deadline:
            return d
        if not args.json:
            v = d.get("verdict") or {}
            played = sum(o.get("W", 0) + o.get("D", 0) + o.get("L", 0) for o in v.get("opponents") or [])
            print(f"  ... {sid} is {d['status']} ({played} games so far)", file=sys.stderr)


def cmd_wait(args):
    d = wait_for(args, args.id, args.timeout)
    emit(args, d, show_submission)


def cmd_show(args):
    emit(args, get(args, f"/api/v1/submissions/{urllib.parse.quote(args.id)}"), show_submission)


def cmd_report(args):
    def human(r):
        print(f"{r['submission']}  {r['games']} games  record {wdl(r['record'])}  ({r['record'].get('score')}%)")
        if r.get("vs_parent"):
            p = r["vs_parent"]
            print(f"vs parent {p['parent']}: {wdl(p)} ({p.get('score')}%)")
        print("\nby opponent (worst first)")
        table([[o["opponent"], wdl(o), o.get("score")] for o in r["by_opponent"]], ["opponent", "W-D-L", "%"])
        print("\nby seat / by how games ended")
        table([[f"seat {k}", wdl(x), x.get("score")] for k, x in r.get("by_seat", {}).items()]
              + [[k, wdl(x), x.get("score")] for k, x in r.get("by_end", {}).items()], ["", "W-D-L", "%"])
        ec = {k: x for k, x in (r.get("economy") or {}).items() if x}
        if ec:
            print("\neconomy (mean dragons alive): " + ", ".join(
                f"{k} {x['dragons']} vs {x['opponent_dragons']}" for k, x in ec.items()))
        print("\nby map (worst first)")
        table([[m["map"], wdl(m), m.get("score")] for m in r["by_map"]], ["map", "W-D-L", "%"])
        print(f"\ndeaths ({r.get('deaths_per_game')} per game, {r.get('errors')} errors)")
        table([[k, v] for k, v in r["deaths"].items()], ["cause", "count"])
        c = r["cpu"]
        print(f"\ncpu: worst p99 {c.get('p99_max')}, max {c.get('max')} of {c['cap']} "
              f"(headroom {c.get('headroom')})")
        if r["worst_losses"]:
            print("\nworst losses")
            table([[g["game"], g["map"], g["opponent"], g["rounds"], g["reason"],
                    f"{arena_url(args)}/games/{g['game']}"] for g in r["worst_losses"]],
                  ["game", "map", "opponent", "rounds", "end", "watch"])
    emit(args, get(args, f"/api/v1/submissions/{urllib.parse.quote(args.id)}/report"), human)


def cmd_promote(args):
    d = request(args, "POST", f"/api/v1/submissions/{urllib.parse.quote(args.id)}/promote")
    emit(args, d, show_submission)


def cmd_leaderboard(args):
    emit(args, get(args, "/api/v1/leaderboard"), lambda rows: table(
        [[r["rank"], r["submission"], r["owner"], r.get("elo"), r.get("err"), r["games"],
          r.get("score"), r["status"]] for r in rows],
        ["#", "submission", "owner", "elo", "±", "games", "%", "status"]))


def cmd_ideas(args):
    def human(rows):
        for i in rows:
            vp = (i.get("verdict") or {}).get("vs_parent") or {}
            print(f"{i['id']:<24} {i['outcome']:<15} {i['owner']}"
                  + (f" / {i['agent']}" if i.get("agent") else "")
                  + (f"  vs parent {wdl(vp)}" if vp else ""))
            if i.get("note"):
                print("    " + i["note"].strip().splitlines()[0][:110])
    emit(args, get(args, "/api/v1/ideas", q=" ".join(args.query), bot=args.bot,
                   outcome=args.outcome, limit=args.limit), human)


def cmd_games(args):
    emit(args, get(args, "/api/v1/games", sub=args.sub, vs=args.vs, map=args.map,
                   suite=args.suite, result=args.result, limit=args.limit),
         lambda rows: table([[g["id"], g["lane"], g["a"], g["b"], g["map"], g["winner"] or "draw",
                              g["rounds"], g["reason"], (g.get("error") or "")[:40]] for g in rows],
                            ["id", "lane", "A", "B", "map", "winner", "rounds", "end", "error"]))


def cmd_get(args):
    blob = request(args, "GET", f"/api/v1/submissions/{urllib.parse.quote(args.id)}/source", raw=True)
    dest = Path(args.dir or args.id)
    if dest.exists() and any(dest.iterdir()):
        raise Fail(f"{dest} exists and isn't empty")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise Fail(f"unsafe path in zip: {info.filename}")
        z.extractall(dest)
    print(f"{args.id} -> {dest}  (submit your change with --parent {args.id})")


def cmd_replay(args):
    blob = request(args, "GET", f"/api/v1/games/{args.game}/replay", raw=True)
    out = Path(args.out or f"game-{args.game}.replay")
    out.write_bytes(blob)
    print(f"{out} ({len(blob) // 1024} KB); watch it at {arena_url(args)}/games/{args.game}")


def cmd_watch(args):
    url = f"{arena_url(args)}/games/{args.game}"
    print(url)
    if not args.no_open:
        webbrowser.open(url)


def cmd_mine(args):
    me = get(args, "/api/v1/whoami")
    rows = get(args, "/api/v1/submissions", owner=me["owner"], agent=args.agent_filter,
               limit=args.limit)
    emit(args, {"me": me, "submissions": rows}, lambda d: (
        print(f"you are {d['me']['owner']}" + (f" (agent label {d['me']['agent']})" if d['me'].get('agent') else "")),
        table([[s["id"], s["lane"], s["status"], s.get("agent"),
                ((s.get("verdict") or {}).get("vs_parent") or {}).get("beats_parent"),
                (s.get("note") or "").strip().splitlines()[0][:60] if s.get("note") else ""]
               for s in d["submissions"]], ["id", "lane", "status", "agent", "beats parent", "note"])))


def cmd_whoami(args):
    emit(args, get(args, "/api/v1/whoami"), lambda d: print(
        f"{d['owner']}" + (f" / {d['agent']}" if d.get("agent") else "")
        + ("  (tagged device: pass --owner to name yourself)" if d.get("tagged") else "")))


def cmd_queue(args):
    def human(q):
        print(f"{len(q['running'])} playing on {q['workers']} workers; checking: {', '.join(q['checking']) or '-'}")
        table([[sid, w["owner"], w["lane"], f"{w['games_left']}/{w['games_total']}"]
               for sid, w in q["waiting"].items()], ["submission", "owner", "lane", "games left"])
    emit(args, get(args, "/api/v1/queue"), human)


def cmd_server(args):
    if args.what == "battles":
        emit(args, get(args, "/api/v1/server/battles", limit=args.limit))
    elif args.what == "leaderboard":
        emit(args, get(args, "/api/v1/server/leaderboard"))
    elif args.what == "quota":
        emit(args, get(args, "/api/v1/server/quota"))
    elif args.what == "gate":
        emit(args, get(args, "/api/v1/server/gate"))
    else:
        emit(args, get(args, "/api/v1/server"))


def cmd_mcp(args):
    url = arena_url(args) + "/mcp"
    print(f"MCP endpoint: {url} (streamable HTTP)\n")
    print("Claude Code:   claude mcp add --transport http tokenz-arena " + url)
    print("Codex:         codex mcp add tokenz-arena --url " + url)
    print('JSON config:   {"mcpServers": {"tokenz-arena": {"type": "http", "url": "' + url + '"}}}')


def cmd_update(args):
    blob = request(args, "GET", "/tokenz.py", raw=True)
    me = Path(sys.argv[0]).resolve()
    me.write_bytes(blob)
    print(f"updated {me}")


# ---------------------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tokenz", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Docs: <arena>/docs  ·  API guide: <arena>/llms.txt")
    p.add_argument("--arena", help="arena URL (default: $TOKENZ_ARENA, ~/.config/tokenz/config.json, "
                                   "or where this file came from)")
    p.add_argument("--json", action="store_true", help="print raw JSON (for agents and scripts)")
    p.add_argument("--agent", help="label which of your agents is calling (shown next to your name)")
    sp = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sp.add_parser("submit", help="zip a bot directory and submit it (a draft by default)")
    s.add_argument("dir", help="the bot's directory (has bot.toml)")
    s.add_argument("--bot", required=True, help="the bot's name; its submissions form a line")
    s.add_argument("--note", help="the hypothesis and what changed (required)")
    s.add_argument("--note-file", help="read the note from a file instead")
    s.add_argument("--parent", help="the submission this builds on, e.g. swarm-6")
    s.add_argument("--lane", choices=["draft", "ranked"], default="draft")
    s.add_argument("--source", help="source link (default: this directory at HEAD on GitHub)")
    s.add_argument("--no-source", action="store_true", help="don't attach a source link")
    s.add_argument("--owner", help="your name, only for shared machines without a Tailscale login")
    s.add_argument("--wait", action="store_true", help="wait for the verdict and print it")
    s.add_argument("--timeout", type=float, default=1800, help="seconds to wait (default 1800)")
    s.set_defaults(f=cmd_submit)

    s = sp.add_parser("wait", help="wait until a submission's games are done, then show it")
    s.add_argument("id")
    s.add_argument("--timeout", type=float, default=1800)
    s.set_defaults(f=cmd_wait)
    s = sp.add_parser("show", help="a submission's status and verdict")
    s.add_argument("id")
    s.set_defaults(f=cmd_show)
    s = sp.add_parser("report", help="where a submission loses: opponents, maps, deaths, CPU")
    s.add_argument("id")
    s.set_defaults(f=cmd_report)
    s = sp.add_parser("promote", help="make a finished draft ranked (it joins the leaderboard)")
    s.add_argument("id")
    s.set_defaults(f=cmd_promote)
    s = sp.add_parser("get", help="download a submission's source to build on")
    s.add_argument("id")
    s.add_argument("dir", nargs="?", help="where to unpack (default: ./<id>)")
    s.set_defaults(f=cmd_get)

    s = sp.add_parser("leaderboard", aliases=["lb"], help="current ranked bots")
    s.set_defaults(f=cmd_leaderboard)
    s = sp.add_parser("ideas", help="search what's been tried, with outcomes")
    s.add_argument("query", nargs="*")
    s.add_argument("--bot")
    s.add_argument("--outcome", choices=["beat_parent", "lost_to_parent", "tied_parent", "no_parent",
                                         "pending", "rejected", "promoted"])
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(f=cmd_ideas)
    s = sp.add_parser("games", help="games, filtered")
    s.add_argument("--sub", help="games of this submission")
    s.add_argument("--vs", help="against this submission")
    s.add_argument("--map")
    s.add_argument("--suite", choices=["official", "rotating", "holdout", "server", "check"])
    s.add_argument("--result", choices=["W", "D", "L"], help="with --sub: only wins/draws/losses")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(f=cmd_games)
    s = sp.add_parser("replay", help="download a game's replay file")
    s.add_argument("game", type=int)
    s.add_argument("-o", "--out")
    s.set_defaults(f=cmd_replay)
    s = sp.add_parser("watch", help="open a game in the browser replay viewer")
    s.add_argument("game", type=int)
    s.add_argument("--no-open", action="store_true", help="just print the URL")
    s.set_defaults(f=cmd_watch)

    s = sp.add_parser("mine", help="your submissions")
    s.add_argument("--agent-filter", metavar="LABEL", help="only this agent's")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(f=cmd_mine)
    s = sp.add_parser("whoami", help="who the arena thinks you are")
    s.set_defaults(f=cmd_whoami)
    s = sp.add_parser("queue", help="what's playing and waiting")
    s.set_defaults(f=cmd_queue)
    s = sp.add_parser("server", help="the competition server: live bot, rank, battles, quota, gate")
    s.add_argument("what", nargs="?", default="status",
                   choices=["status", "battles", "leaderboard", "quota", "gate"])
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(f=cmd_server)
    s = sp.add_parser("mcp", help="how to add the arena's MCP server to your agent")
    s.set_defaults(f=cmd_mcp)
    s = sp.add_parser("update", help="replace this file with the arena's latest tokenz.py")
    s.set_defaults(f=cmd_update)

    args = p.parse_args(argv)
    global DEFAULT_URL_HINT
    DEFAULT_URL_HINT = arena_url(args)
    try:
        return args.f(args) or 0
    except Fail as e:
        print(f"tokenz: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
