"""Keeping replays from filling the disk (DESIGN.md section 5).

- Replays are stored gzip-compressed (3-4x smaller) under their usual name, <id>.replay; the
  website serves them with Content-Encoding: gzip, so browsers (and the viewer) unpack them.
- compress_existing() gzips older uncompressed replays in place, a batch at a time.
- prune() keeps the replay folder under [retention] replay_cap_gb (default 40): it deletes the
  oldest local games' replays first. Real competition-server games' replays are never deleted.
  A pruned game keeps its verdict and stats; its page says the replay isn't kept.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("arena")
MAGIC = b"\x1f\x8b"


def is_gzip(p: Path) -> bool:
    with p.open("rb") as f:
        return f.read(2) == MAGIC


def compress_file(src: Path, dest: Path) -> None:
    """gzip src into dest (atomically); src may equal dest."""
    tmp = dest.with_name(dest.name + ".gz-tmp")
    with src.open("rb") as fi, gzip.open(tmp, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    os.replace(tmp, dest)
    if src != dest and src.exists():
        src.unlink()


def compress_existing(arena, batch: int = 200) -> int:
    """gzips up to `batch` uncompressed replays (newest last); returns how many."""
    done = 0
    for p in sorted(arena.cfg.replays.glob("*.replay"), key=lambda p: p.stat().st_mtime):
        if done >= batch:
            break
        try:
            if not is_gzip(p):
                compress_file(p, p)
                done += 1
        except FileNotFoundError:
            continue
    if done:
        log.info("retention: compressed %d replays", done)
    return done


def prune(arena) -> int:
    cap = float(arena.cfg.get("retention", "replay_cap_gb", 40)) * 2**30
    files = {p.name: p for p in arena.cfg.replays.glob("*.replay")}
    total = sum(p.stat().st_size for p in files.values())
    if total <= cap:
        return 0
    target = cap * 0.85
    removed = 0
    with arena.lock:
        candidates = [g for g in arena.games if g.replay and g.lane != "server"]   # oldest first
    for g in candidates:
        if total <= target:
            break
        p = files.get(g.replay)
        if p is None:
            continue
        try:
            size = p.stat().st_size
            p.unlink()
        except FileNotFoundError:
            continue
        total -= size
        g.replay = None
        arena.save_game(g)
        removed += 1
    log.info("retention: removed %d old replays; replays now %.1f GB (cap %.0f GB)",
             removed, total / 2**30, cap / 2**30)
    return removed


def install(daemon):
    a = daemon.arena
    daemon.every(60, lambda: compress_existing(a), "replay-compress")
    daemon.every(300, lambda: prune(a), "replay-prune")
