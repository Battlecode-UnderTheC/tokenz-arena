"""arena-metrics (native/metrics): the same metrics in C++, about 50x faster than Python.

It's used only when it has exactly the metrics the Python plugins register, at the same versions
(`usable()`): a metric added or changed in Python runs in Python, everywhere, until its C++ twin
is written and the parity check (native/metrics/parity.py) passes. So the Python plugins stay the
reference and the place to try a metric first; this is only a faster way to compute the same
numbers.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("arena")
ROOT = Path(__file__).resolve().parents[2]
BINARIES = [Path("/opt/arena-metrics/arena-metrics"), ROOT / "native" / "metrics" / "build" / "arena-metrics"]


def binary() -> Path | None:
    return next((b for b in BINARIES if b.is_file() and os.access(b, os.X_OK)), None)


@lru_cache(maxsize=1)
def describe() -> tuple[dict, ...]:
    b = binary()
    if b is None:
        return ()
    try:
        out = subprocess.run([str(b), "--describe"], capture_output=True, text=True, timeout=30, check=True).stdout
        return tuple(json.loads(out))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning("arena-metrics --describe failed (%s): metrics run in Python", e)
        return ()


_told: set = set()


def usable() -> bool:
    """Whether the binary computes exactly the registered metrics (names and versions). Checked
    against the registry each time, so a metric registered later runs in Python."""
    from arena import metrics
    have = {m["name"]: m["version"] for m in describe()}
    want = metrics.versions()
    if have and have != want:
        missing = tuple(sorted(k for k in set(want) | set(have) if have.get(k) != want.get(k)))
        if missing not in _told:
            _told.add(missing)
            log.info("arena-metrics differs from the Python metrics on %s: metrics run in Python", list(missing))
    return bool(have) and have == want


def compute(blob: bytes) -> dict:
    """compute() for one replay's bytes, in C++. Raises on any failure (the caller falls back)."""
    r = subprocess.run([str(binary()), "-"], input=blob, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:300] or f"exit {r.returncode}")
    return json.loads(r.stdout)


def compute_files(pairs: list[tuple[Path, Path]], threads: int | None = None, nice: int = 0) -> dict[str, str]:
    """Each (replay, out.json) pair's metrics written to out.json, on `threads` threads (all cores
    by default). Returns {replay: "ok" or the error}; out.json holds {"error": ...} for a replay
    that couldn't be read, as the Python backfill writes."""
    if not pairs:
        return {}
    cmd = [str(binary())] + (["-j", str(threads)] if threads else [])
    if nice:
        cmd = ["nice", "-n", str(nice)] + cmd
    lines = "".join(f"{r}\t{o}\n" for r, o in pairs)
    r = subprocess.run(cmd, input=lines, capture_output=True, text=True, timeout=3600)
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith("ok "):
            out[line[3:]] = "ok"
        elif line.startswith("error "):
            path, _, why = line[6:].partition(": ")
            out[path] = why or "error"
    return out
