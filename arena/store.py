"""State as plain JSON files, one per record, under data_dir. No database.

    submissions/<id>/meta.json      the submission (status, owner, parent, note, checks)
    submissions/<id>/source.zip     the uploaded bot, kept forever
    games/<id>.json                 one finished game: seats, map, seed, verdict, stats
    replays/<game id>.replay        pruned by retention; the game file keeps its verdict
    tokens.json                     owner -> sha256 of their submit token
    ratings.json                    the latest Bradley-Terry fit (derived; safe to delete)

Only the arena process writes, and every write is a temp file plus an atomic rename, so a
crash never leaves half a file. On start the process loads everything into memory; the
queue isn't stored, it's rebuilt from what each submission still owes.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def load_all(directory: Path, pattern: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob(pattern))]
