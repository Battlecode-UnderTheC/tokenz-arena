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
    daemon = Daemon(arena)
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
    p = sub.add_parser("serve", help="the arena: HTTP API, website, MCP, judge workers, server bridge")
    p.add_argument("--config", help="arena.toml (default: the one in this checkout, if any)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=_serve)
    p = sub.add_parser("status", help="the leaderboard in the terminal")
    p.add_argument("--url", default="http://127.0.0.1:8700")
    p.set_defaults(func=_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
