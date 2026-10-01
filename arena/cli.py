"""The `arena` command."""

from __future__ import annotations

import argparse
import json
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
    for name, what in [("serve", "the arena: HTTP API, MCP, scheduler and judge workers"),
                       ("status", "the leaderboard in the terminal"),
                       ("import-server", "import a competition battle's replays")]:
        sub.add_parser(name, help=f"{what} (not built yet)").set_defaults(func=None)
    args = parser.parse_args(argv)
    if args.func is None:
        print(f"arena {args.cmd}: not built yet (see DESIGN.md, build order)", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
