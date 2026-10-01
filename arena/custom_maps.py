"""Team-uploaded maps: the `custom` suite every bot plays on, or (hidden) the holdout.

A map is checked by playing one game on it with the toolkit's template bot under the
official judge, so anything the judge can't load is refused with its message. Accepted maps
go to data_dir/maps/custom/<name>.map (public: listed, previewable, in drafts, placement and
continuous games) or data_dir/maps/holdout-custom/<name>.map (hidden: part of the holdout
suite, so only the top 4 play it, and nobody sees it or its games). Their metadata is in data_dir/maps/custom.json.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

from arena import runner, store
from arena.state import Arena, now

MAX_BYTES = 512 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")


class Refused(ValueError):
    pass


def _meta_path(arena: Arena) -> Path:
    return arena.cfg.data_dir / "maps" / "custom.json"


def meta(arena: Arena) -> dict:
    return store.read_json(_meta_path(arena), {}) or {}


def sample_bot(arena: Arena) -> Path:
    """The toolkit's Python template bot, made once: the opponent for map checks."""
    d = arena.cfg.data_dir / "tools" / "sample-bot"
    if not (d / "bot.toml").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["unswbc", "init", "python", str(d)], check=True, capture_output=True,
                       timeout=120)
    return d


def _clean_name(name: str, filename: str) -> str:
    name = (name or Path(filename or "").stem).strip().lower().replace(" ", "_")
    if not _NAME.match(name):
        raise Refused("map name: 2-48 chars of a-z, 0-9, '_' and '-', starting with a letter or digit")
    return name


def upload(arena: Arena, *, data: bytes, owner: str, name: str = "", filename: str = "",
           note: str = "", hidden: bool = False) -> dict:
    if len(data) > MAX_BYTES:
        raise Refused(f"map file is {len(data) // 1024} KB; the limit is {MAX_BYTES // 1024} KB")
    try:
        text = data.decode()
    except UnicodeDecodeError:
        raise Refused("a map file is text")
    first = next((ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")), "")
    if not re.match(r"^MAP\s+\d+\s+\d+", first):
        raise Refused("not an unswbc map: the first line should be 'MAP <width> <height>'")
    name = _clean_name(name, filename)
    m = meta(arena)
    taken = {p.stem.lower() for s in ("official", "tournament", "server")
             for d in [arena.cfg.map_dir(s)] if d and d.is_dir() for p in d.glob("*.map")}
    if name in m or name in taken:
        raise Refused(f"there's already a map named {name!r}; pick another name")
    sha = hashlib.sha256(data).hexdigest()
    if any(x.get("sha256") == sha for x in m.values()):
        raise Refused("this exact map was already uploaded")
    # The official judge decides whether it's a valid map.
    bot = sample_bot(arena)
    with tempfile.TemporaryDirectory(dir=arena.cfg.work if arena.cfg.work.exists() else None) as tmp:
        path = Path(tmp, f"{name}.map")
        path.write_text(text)
        try:
            r = runner.play(path, bot, bot, arena.cfg.seeds("official")[0], Path(tmp, "replay"),
                            timeout=600)
        except runner.JudgeError as e:
            raise Refused("the judge couldn't play this map: " + str(e).splitlines()[-1][:300])
    suite = "holdout" if hidden else "custom"
    dest = arena.cfg.map_dir("holdout_custom" if hidden else "custom")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{name}.map").write_text(text)
    entry = {"name": name, "suite": suite, "owner": owner, "note": note, "created": now(),
             "sha256": sha, "size": first.split()[1:3],
             "check": f"{r.winner or 'draw'} after {r.rounds} rounds ({r.reason})"}
    m[name] = entry
    store.write_json(_meta_path(arena), m)
    if getattr(arena, "daemon", None):
        arena.daemon.wake.set()
    return entry


def remove(arena: Arena, name: str, by: str) -> dict:
    m = meta(arena)
    entry = m.get(name)
    if not entry:
        raise Refused(f"no uploaded map {name!r}")
    if entry["owner"] != by and by != "local":
        raise Refused(f"only {entry['owner']} can remove {name!r}")
    folder = "holdout_custom" if entry["suite"] == "holdout" else "custom"
    (arena.cfg.map_dir(folder) / f"{name}.map").unlink(missing_ok=True)
    del m[name]
    store.write_json(_meta_path(arena), m)
    return {**entry, "removed_by": by}


def listing(arena: Arena) -> list[dict]:
    """Every public map by suite; hidden uploads only as a count, never by name."""
    m = meta(arena)
    out = []
    from arena.maps import suite_files
    for suite in ("server", "official", "tournament", "custom", "rotating"):
        for p in suite_files(arena.cfg, suite):
            row = {"suite": suite, "name": p.stem, "preview": f"/maps/{suite}/{p.stem}"}
            if suite == "custom" and p.stem in m:
                row.update({k: m[p.stem][k] for k in ("owner", "note", "created", "size")})
            out.append(row)
    return out


def hidden_count(arena: Arena) -> int:
    return sum(1 for x in meta(arena).values() if x["suite"] == "holdout")
