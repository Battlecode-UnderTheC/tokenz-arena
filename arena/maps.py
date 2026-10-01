"""Map suites, the way the syeyoung-tokens league does it (its ORCHESTRATOR.md):

| suite      | maps                                                   | where                         |
|------------|--------------------------------------------------------|-------------------------------|
| official   | the official maps                                      | <repo>/maps/official          |
| tournament | competition tournament maps (rebuilt from replays)     | <repo>/maps/tournament        |
| server     | the maps the competition server plays now (bridge)     | data_dir/maps/server          |
| rotating   | 20 worldgen maps, a new set every week (epoch)         | data_dir/maps/rotating/e<N>   |
| holdout    | 60 worldgen maps from a secret seed range, never shown | data_dir/maps/holdout         |

worldgen.py (a procedural map generator by xCirno1) is published as a gist with no license,
so it's not copied into this repo: it's downloaded from the gist at a pinned revision and
checked against its sha256.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import logging
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from arena.config import Config

log = logging.getLogger("arena")

WORLDGEN_URL = ("https://gist.githubusercontent.com/xCirno1/ffdaac4236c1f1085c351af4fdfc1600/raw/"
                "2d49ffd7dba611fbfe8b27e5d770a90359ee34ec/worldgen.py")
WORLDGEN_SHA256 = "7134bfe6cd24e5efff48a277b618db5618dea569dce6e600bbeb8a93e2673ee2"
ROTATING_MAPS = 20
EPOCH_DAYS = 7
EPOCH_START = 1767225600          # 2026-01-01T00:00:00Z
HOLDOUT_MAPS = 60


def epoch(now: float | None = None) -> int:
    return int(((now or time.time()) - EPOCH_START) // (EPOCH_DAYS * 86400))


def worldgen(cfg: Config) -> Path:
    """The generator script, downloaded once into data_dir/tools/ and verified."""
    p = cfg.data_dir / "tools" / "worldgen.py"
    if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == WORLDGEN_SHA256:
        return p
    data = urllib.request.urlopen(WORLDGEN_URL, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != WORLDGEN_SHA256:
        raise RuntimeError("worldgen.py from the gist doesn't match the pinned sha256")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _generate(cfg: Config, seeds: list[int], out: Path, prefix: str):
    gen = worldgen(cfg)
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    def one(seed: int):
        subprocess.run([sys.executable, str(gen), "--seed", str(seed), "-o",
                        str(tmp / f"{prefix}{seed}.map")], check=True, capture_output=True, timeout=300)

    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(one, seeds))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)


def rotating_dir(cfg: Config, e: int | None = None) -> Path:
    base = cfg.path(cfg.get("maps", "rotating", "maps/rotating"))
    return base / f"e{epoch() if e is None else e}"


def ensure_rotating(cfg: Config) -> Path:
    """This epoch's rotating suite, generated if missing (seeds e*1000 .. e*1000+19)."""
    e = epoch()
    d = rotating_dir(cfg, e)
    n = cfg.get("maps", "rotating_count", ROTATING_MAPS)
    if not d.is_dir() or len(list(d.glob("*.map"))) < n:
        log.info("maps: generating rotating suite e%d (%d worldgen maps)", e, n)
        _generate(cfg, [e * 1000 + i for i in range(n)], d, "wg")
    return d


def ensure_holdout(cfg: Config) -> Path:
    """The hidden holdout: worldgen maps from a secret seed range chosen once per arena.
    Never served: no API, page or MCP tool lists these maps or their games."""
    d = cfg.map_dir("holdout")
    n = cfg.get("maps", "holdout_count", HOLDOUT_MAPS)
    if d.is_dir() and len(list(d.glob("*.map"))) >= n:
        return d
    salt_file = d.with_name("holdout.salt")
    if not salt_file.exists():
        salt_file.parent.mkdir(parents=True, exist_ok=True)
        salt_file.write_text(str(10_000_000 + secrets.randbelow(900_000_000)))
    salt = int(salt_file.read_text())
    log.info("maps: generating the holdout suite (%d worldgen maps)", n)
    _generate(cfg, [salt + i for i in range(n)], d, "h")
    return d


def ensure_all(cfg: Config):
    for fn in (ensure_rotating, ensure_holdout):
        try:
            fn(cfg)
        except Exception as e:      # no network, say: the other suites still work
            log.warning("maps: %s failed: %s", fn.__name__, e)
