"""The `arena` command."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from arena import runner


def _play(args: argparse.Namespace) -> int:
    result = runner.play(Path(args.map), Path(args.a), Path(args.b), args.seed, Path(args.out))
    summary = asdict(result)
    summary.pop("log")
    summary["replay"] = str(result.replay) if result.replay else None
    print(json.dumps(summary, indent=2))
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from arena import config
    from arena.api.http import create_app
    from arena.daemon import Daemon
    from arena.state import Arena

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    arena = Arena(cfg)
    from arena.migrate import stamp_map_shas
    n = stamp_map_shas(arena)
    if n:
        logging.getLogger("arena").info("stamped %d older games with their map's sha256", n)
    daemon = Daemon(arena)
    if cfg.get("maps", "worldgen", True):
        daemon.install_maps()
    try:
        from arena.server import bridge
        if hasattr(bridge, "install"):
            bridge.install(arena, daemon)
    except ModuleNotFoundError:
        pass
    daemon.start()
    app = create_app(arena)
    uvicorn.run(app, host=args.host or cfg.get("api", "host", "127.0.0.1"),
                port=args.port or cfg.get("api", "port", 8700), log_level="warning")
    return 0


def _engine(args: argparse.Namespace) -> int:
    """`arena run`: the engine. The only writer: scheduler, judge worker processes, map
    generation and the server bridge, with the full API on an internal address that the
    website forwards writes to."""
    import uvicorn

    from arena import config
    from arena.api.http import create_app
    from arena.daemon import Daemon
    from arena.state import Arena

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    arena = Arena(cfg)
    from arena.migrate import stamp_map_shas
    n = stamp_map_shas(arena)
    if n:
        logging.getLogger("arena").info("stamped %d older games with their map's sha256", n)
    daemon = Daemon(arena)
    if cfg.get("maps", "worldgen", True):
        daemon.install_maps()
    try:
        from arena.server import bridge
        bridge.install(arena, daemon)
    except ModuleNotFoundError:
        pass
    from arena import retention, workers
    from arena.metrics import backfill
    workers.install(arena, daemon)
    retention.install(daemon)
    backfill.install(daemon)
    daemon.start()
    uvicorn.run(create_app(arena), host="127.0.0.1",
                port=args.port or cfg.get("api", "engine_port", 8701), log_level="warning")
    return 0


def _web(args: argparse.Namespace) -> int:
    """`arena web`: the website, API and MCP, from a read-only copy of the state refreshed
    every second; writes go to the engine. Runs no games, so pages stay fast."""
    import threading
    import time

    import uvicorn

    from arena import config
    from arena.api.http import create_app
    from arena.state import Arena

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    arena = Arena(cfg, readonly=True)
    arena.refresh()

    def follow():
        n = 0
        while True:
            time.sleep(1)
            n += 1
            try:
                arena.refresh(full=(n % 120 == 0))
            except Exception:
                logging.getLogger("arena").exception("refresh failed")
    threading.Thread(target=follow, daemon=True, name="refresh").start()
    engine = f"http://127.0.0.1:{cfg.get('api', 'engine_port', 8701)}"
    uvicorn.run(create_app(arena, engine_url=engine), host=args.host or cfg.get("api", "host", "127.0.0.1"),
                port=args.port or cfg.get("api", "port", 8700), log_level="warning")
    return 0


def _migrate(args: argparse.Namespace) -> int:
    from arena import config, migrate
    cfg = config.load(Path(args.config) if args.config else None)
    rep = migrate.rename_submissions(cfg, Path(args.table), dry_run=not args.apply,
                                     remove=args.remove)
    print(json.dumps(rep, indent=1))
    if not args.apply:
        print("dry run: nothing written (add --apply, with the engine stopped)")
    return 0


def _status(args: argparse.Namespace) -> int:
    import urllib.request
    url = args.url.rstrip("/") + "/api/v1/leaderboard"
    rows = json.load(urllib.request.urlopen(url, timeout=10))
    print(f"{'#':>3}  {'submission':<24} {'owner':<20} {'elo':>7} {'±':>5} {'games':>5} {'score':>6}  status")
    for r in rows:
        print(f"{r['rank']:>3}  {r['submission']:<24} {r['owner'][:20]:<20} {r['elo'] or '-':>7} "
              f"{r['err'] or '-':>5} {r['games']:>5} {r['score'] or '-':>6}  {r['status']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arena")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("play", help="one game with the official judge, printed as JSON")
    p.add_argument("map")
    p.add_argument("a", help="bot directory for team A")
    p.add_argument("b", help="bot directory for team B")
    p.add_argument("--seed", type=int, default=1592614637)
    p.add_argument("--out", default="replays")
    p.set_defaults(func=_play)
    p = sub.add_parser("serve", help="everything in one process (local use; production runs `run` + `web`)")
    p.add_argument("--config", help="arena.toml (default: the one in this checkout, if any)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=_serve)
    p = sub.add_parser("run", help="the engine: scheduler, judge workers, bridge (internal API)")
    p.add_argument("--config")
    p.add_argument("--port", type=int, help="internal API port (default [api] engine_port, 8701)")
    p.set_defaults(func=_engine)
    p = sub.add_parser("web", help="the website, API and MCP (reads a copy; writes go to the engine)")
    p.add_argument("--config")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=_web)
    p = sub.add_parser("worker", help="a remote judge: lease games from an arena and play them here")
    p.add_argument("--arena", help="the arena's URL (or ARENA_URL)")
    p.add_argument("--token", help="a worker token (or ARENA_WORKER_TOKEN)")
    p.add_argument("--name", help="this worker's name (default: the host name)")
    p.add_argument("--slots", type=int, help="games at once (default: cores / 1.5)")
    p.add_argument("--cache", help="bots, maps and work files (default ~/.cache/tokenz-arena-worker, or ARENA_CACHE)")
    p.add_argument("--idle-exit", type=float, help="exit after this many seconds with nothing to do "
                                                    "(or ARENA_IDLE_EXIT; for batch jobs)")
    p.set_defaults(func=lambda a: __import__("arena.worker", fromlist=["main"]).main(a))
    p = sub.add_parser("tailscale-key", help="mint the Tailscale auth key remote workers join with")
    p.add_argument("--dir", default="~/.config/tokenz-arena",
                   help="holds tailscale-oauth-id and tailscale-oauth (the OAuth client)")
    p.add_argument("--tag", default="tag:tokens-worker")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", help="where to write the key (default <dir>/worker-ts-authkey)")
    p.set_defaults(func=lambda a: __import__("arena.tskey", fromlist=["main"]).main(a))
    p = sub.add_parser("migrate-renames", help="move submissions into lines from a TSV (engine stopped)")
    p.add_argument("table", help="TSV: old_id new_bot new_id team lane")
    p.add_argument("--config")
    p.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    p.add_argument("--remove", action="append", default=[], metavar="ID",
                   help="delete this submission first (moved to data_dir/removed/); repeatable")
    p.set_defaults(func=_migrate)
    p = sub.add_parser("evals", help="the evals service: real games between other teams vs our metrics")
    p.add_argument("--config")
    p.add_argument("--import", dest="import_dir", metavar="DIR",
                   help="store battles crawled earlier (DIR/M<battle>/battle.json + replays) and exit")
    p.set_defaults(func=lambda a: __import__("arena.evals.collect", fromlist=["main"]).main(a))
    p = sub.add_parser("status", help="the leaderboard in the terminal")
    p.add_argument("--url", default="http://127.0.0.1:8700")
    p.set_defaults(func=_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
