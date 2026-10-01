"""One-off data migrations, run with the engine stopped (`arena migrate-renames TSV`).

rename_submissions: moves submissions into lines from a table of
    old_id  new_bot  new_id  team  lane
- each submission gets its new id and bot (its folder and unpacked bot folder are renamed);
- every reference follows: parents (and their history), frozen opponents, games' seats,
  bots.json, ratings.json and the server bridge's version links;
- the old id becomes an alias (aliases.json), so links, notes and API calls with it still work;
- the agent label 'repo-import' becomes the row's team;
- a line ends up with at most one current ranked submission: the one with the highest version
  number among the old current entries; the others are retired (by 'migration').
"""

from __future__ import annotations

import csv
import re
from dataclasses import asdict
from pathlib import Path

from arena import store
from arena.config import Config
from arena.state import RETIRED, Arena, now


def _version(sid: str) -> tuple:
    m = re.search(r"(\d+)([a-z]*)$", sid)
    return (int(m.group(1)), m.group(2)) if m else (-1, "")


def rename_submissions(cfg: Config, table: Path, dry_run: bool = True) -> dict:
    arena = Arena(cfg)
    rows = [r for r in csv.DictReader(table.open(), delimiter="\t") if r.get("old_id")]
    ren = {r["old_id"]: r for r in rows}
    missing = [o for o in ren if o not in arena.subs]
    if missing:
        raise SystemExit(f"not in the arena: {', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}")
    new_ids = [r["new_id"] for r in rows]
    dupes = {x for x in new_ids if new_ids.count(x) > 1}
    clash = [n for n in new_ids if n in arena.subs and n not in ren]
    if dupes or clash:
        raise SystemExit(f"new ids collide: {sorted(dupes)[:10]} {clash[:10]}")
    idmap = {o: r["new_id"] for o, r in ren.items() if r["new_id"] != o}
    fix = lambda sid: idmap.get(sid, sid)

    old_current = {b.get("current") for b in arena.bots.values() if b.get("current")}
    report = {"renamed": len(idmap), "games": 0, "retired": [], "lines": {}}

    # submissions: id, bot, agent label, and every reference to another submission
    for s in list(arena.subs.values()):
        r = ren.get(s.id)
        if r:
            s.bot = r["new_bot"]
            if s.agent in (None, "", "repo-import") and r.get("team"):
                s.agent = r["team"]
        s.parent = fix(s.parent)
        if s.opponents is not None:
            s.opponents = [fix(x) for x in s.opponents]
        if s.quick_opponents is not None:
            s.quick_opponents = [fix(x) for x in s.quick_opponents]
        for h in s.parent_history:
            h["parent"] = fix(h.get("parent"))
    # one current ranked submission per line
    lines: dict[str, list] = {}
    for s in arena.subs.values():
        if s.id in old_current:
            lines.setdefault(s.bot, []).append(s)
    # bots: keep any that still has submissions (current re-pointed), drop the emptied
    # per-variant bots, add the lines
    used = {x.bot for x in arena.subs.values()}
    new_bots: dict[str, dict] = {name: {**b, "current": None} for name, b in arena.bots.items()
                                 if name in used}
    for x in arena.subs.values():
        new_bots.setdefault(x.bot, {"owner": x.owner, "active": True, "current": None})
    for name, cur in lines.items():
        keep = max(cur, key=lambda s: _version(fix(s.id)))
        new_bots[name]["current"] = fix(keep.id)
        for s in cur:
            if s is not keep:
                s.status, s.retired_at, s.retired_by = RETIRED, now(), "migration"
                report["retired"].append(f"{s.id} -> {fix(s.id)}")
        report["lines"][name] = fix(keep.id)
    # games
    changed_games = []
    for g in arena.games:
        a, b = fix(g.a), fix(g.b)
        if (a, b) != (g.a, g.b):
            g.a, g.b = a, b
            changed_games.append(g)
    report["games"] = len(changed_games)
    if dry_run:
        report["dry_run"] = True
        return report

    # write: folders, metas, games, bots, ratings, bridge links, aliases
    # two phases, via temporary names: a table may give a freed id to another submission
    for phase in ("tmp", "final"):
        for old, new in idmap.items():
            src, dst = (old, f".mig-{old}") if phase == "tmp" else (f".mig-{old}", new)
            for base in (cfg.submissions, cfg.bots):
                if (base / src).exists():
                    (base / src).rename(base / dst)
    for s in list(arena.subs.values()):
        s.id = fix(s.id)
        store.write_json(cfg.submissions / s.id / "meta.json", asdict(s))
    for g in changed_games:
        store.write_json(cfg.games / f"{g.id}.json", asdict(g))
    store.write_json(cfg.data_dir / "bots.json", new_bots)
    ratings = store.read_json(cfg.data_dir / "ratings.json", {}) or {}
    store.write_json(cfg.data_dir / "ratings.json", {fix(k): v for k, v in ratings.items()})
    vfile = cfg.data_dir / "server" / "versions.json"
    versions = store.read_json(vfile, None)
    if versions:
        for v in versions.values():
            if isinstance(v, dict) and v.get("arena"):
                v["arena"] = fix(v["arena"])
        store.write_json(vfile, versions)
    aliases = store.read_json(cfg.data_dir / "aliases.json", {}) or {}
    aliases = {k: fix(v) for k, v in aliases.items()}
    aliases.update(idmap)
    taken = {fix(x) for x in ren} | set(idmap.values())
    aliases = {k: v for k, v in aliases.items() if k not in taken}   # an id in use isn't an alias
    store.write_json(cfg.data_dir / "aliases.json", aliases)
    report["dry_run"] = False
    return report
