"""Evals (docs/EVALS.md): checks our in-game metrics against real games between other teams.

`arena evals` is its own process (compose service `evals`), apart from the engine and the
website. It polls the competition server for finished battles between the top teams, downloads
their replays, computes every metric (arena/metrics) for each game, and keeps a report of how
well the eval and each metric predict who actually won. It writes only under data_dir/evals:

    battles/<battle id>.json    the battle as the server gave it (and how it was found)
    games/<game id>.json        one game: battle, time, map, both teams, seats, winner, end
    metrics/<game id>.json      arena.metrics.compute() of its replay
    replays/<game id>.replay    as served (gzip); oldest pruned past [evals] keep_gb
    report.json                 the report (arena/evals/report.py)
    status.json                 the collector's heartbeat: last poll, counts, last error

The website reads these files; nothing here goes through the engine.
"""

from __future__ import annotations

from pathlib import Path


def evals_dir(cfg) -> Path:
    return cfg.data_dir / "evals"
