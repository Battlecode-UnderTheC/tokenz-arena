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


def rename_submissions(cfg: Config, table: Path, dry_run: bool = True,
                       remove: list[str] | None = None) -> dict:
    """`remove`: submissions to delete first (their folders, unpacked bots and games are moved to
    data_dir/removed/, so it can be undone by hand), e.g. ones whose ids the table needs."""
    arena = Arena(cfg)
    remove = [r for r in (remove or []) if r]
    unknown = [r for r in remove if r not in arena.subs]
    if unknown:
        raise SystemExit(f"can't remove, not in the arena: {unknown}")
    removed_games = [g for g in arena.games if g.a in remove or g.b in remove]
    kids = [s.id for s in arena.subs.values() if s.parent in remove]
    for sid in remove:
        dict.pop(arena.subs, sid, None)
    arena.games = [g for g in arena.games if g not in removed_games]
    for s in arena.subs.values():
        if s.parent in remove:
            s.parent = None
        if s.opponents:
            s.opponents = [o for o in s.opponents if o not in remove]
        if s.quick_opponents:
            s.quick_opponents = [o for o in s.quick_opponents if o not in remove]
    for b in arena.bots.values():
        if b.get("current") in remove:
            b["current"] = None
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
    report = {"renamed": len(idmap), "games": 0, "retired": [], "lines": {},
              "removed": {"submissions": remove, "games": len(removed_games), "orphaned_children": kids}}

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

    # write: removals first, then folders, metas, games, bots, ratings, bridge links, aliases
    trash = cfg.data_dir / "removed"
    for sid in remove:
        for base, name in ((cfg.submissions, "submissions"), (cfg.bots, "bots")):
            if (base / sid).exists():
                (trash / name).mkdir(parents=True, exist_ok=True)
                (base / sid).rename(trash / name / sid)
    for g in removed_games:                   # a copy in removed/, then out of games.db
        (trash / "games").mkdir(parents=True, exist_ok=True)
        store.write_json(trash / "games" / f"{g.id}.json", asdict(g))
    if removed_games:
        arena.gamedb.delete([g.id for g in removed_games])
    for s in arena.subs.values():
        if s.id not in ren:
            store.write_json(cfg.submissions / s.id / "meta.json", asdict(s))   # cleared refs
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
    if changed_games:
        arena.gamedb.put_many([asdict(g) for g in changed_games])
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


# ---------------------------------------------------------------------------- map versions
# The repo's official and tournament maps as every game before map versions was played on
# them (the files never changed from the arena's first commit to unswbc 1.2.7's maps).
LEGACY_MAP_SHA = {
    ("official", "Colosseum"): "c7549a41ea26600f613a7d906e5c73e90303f3bc037b28d8aa2751cd17819772",
    ("official", "arena"): "b4bab09603289c2d186ca94d6cf5362e5cc8f24637736c497d9651c0dd921937",
    ("official", "autarky"): "35737755c5a3123fcd4c3fd1cf1ecb08513adf9ac14c351f00f76c23e9bd6dbd",
    ("official", "big_empty"): "6cb3b7be72a4e27ed19363e9009d46742851709f701e61d73b56437314fdd827",
    ("official", "default"): "6bd7abf2ed51a7f667552c243ad038f4177e450cf2f9dd9be52f826f9e526f95",
    ("official", "default_small"): "8ac4b2eedcf19f8544ab1866acfcbc628b3cb5ff2df87d80aa682dcb5aa9df19",
    ("official", "devil"): "a09e8f2d3bdd1e40fad31a0ecdf00166cd1090c232c56dcc2557729de737f6aa",
    ("official", "dilemma"): "d76d7ad9ada3a8d089b52f4a973e7e9acf7ae4b7f695e9f018caf6ae75bdb109",
    ("official", "portals"): "56341e6577da7429efb6f33b64b79b0c4223c4fbd9df995713ad781bead5f423",
    ("official", "queen_of_spades"): "126d71b46443268596285918d910b7f8d875bf3028ca9c5ff6bad97b4e868938",
    ("official", "schooltime"): "726c404f3b34b4ed7b21cef83628407e70b717cd919f114e4a0e57f7f6d66520",
    ("official", "slithery_fight"): "954ea0147fbc56532920caa51ecda03f21ef6765806d7adc0eef73f6069001bc",
    ("official", "stronghold"): "c83cb7579e89fff0064b075f38250fa06487a10b6ddf320f6c57d63a84e9f32c",
    ("official", "trauma"): "dcf39d713c060e5ded2d031a7ffe1a27ea60fe4b72ff8fb3fffbabc4f81e2066",
    ("official", "trophy"): "5f1f6b23d6edb944954a9a31b95f229fd9966bec0875a9f04bca680aa3de33f5",
    ("tournament", "around_unsw"): "7fbbc4f6407a3e6ea55bb18b94dfc1f7363c7a59f7a01766815ea9fcad7b12f4",
    ("tournament", "australia"): "8bfe33cd0bfd01406ad55c6e139c520b16312a58199747c371648f1137d222f7",
    ("tournament", "islands"): "8c1494d9001cbd74e44ade6a35f82b5f69a66e1afdc3fa5b91a4c0eb1eb4959b",
    ("tournament", "maze"): "1379c545f5e6ad6d7aa2e853b63060ea7b08bba2e12aa6976af7d3a42840b3e4",
    ("tournament", "stripes"): "cc5a85c0ff9e4676836ee70278b668ea2555ac130e75db93865f9f96c5d8f21d",
    ("tournament", "tower_defense"): "ad590eaf3a52abb0bc16b2fdb606d0b79e9fabafd893925fc7c85c0f21ad8327",
    ("tournament", "weakhold"): "23adc2c88dca1aa1cfdfe7eabe928733a27c37ab9bbd3d4ee2d0947e7b5e103a",
}


def stamp_map_shas(arena: Arena) -> int:
    """Gives every game recorded before map versions its map's sha256 (meta.map_sha), so
    state.current_map can tell games on a since-changed map. Idempotent; run by the engine at
    start. Repo maps: LEGACY_MAP_SHA. The server's maps change in place (the bridge rewrites a
    file when the server's copy changes): a game played before its file's last change was on an
    older version ('older'). Other suites' files never change: their current hash."""
    from datetime import datetime

    from arena.maps import file_sha

    def ts(at: str) -> float:
        try:
            return datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    def find(suite: str, stem: str) -> Path | None:
        if suite == "rotating":
            base = arena.cfg.data_dir / "maps" / "rotating"
            hits = sorted(base.glob(f"*/{stem}.map")) if base.exists() else []
            return hits[-1] if hits else None
        for d in ([arena.cfg.map_dir(suite)] + ([arena.cfg.map_dir("holdout_custom")] if suite == "holdout" else [])):
            if d and (d / f"{stem}.map").exists():
                return d / f"{stem}.map"
        return None

    n = 0
    for g in list(arena.games):
        meta = g.meta if g.meta is not None else {}
        if "map_sha" in meta or g.lane == "server":
            continue
        sha = LEGACY_MAP_SHA.get((g.suite, g.map))
        if sha is None:
            p = find(g.suite, g.map)
            if p is None:
                sha = "unknown"
            elif g.suite == "server" and ts(g.played) < p.stat().st_mtime:
                sha = "older"
            else:
                sha = file_sha(p)
        meta["map_sha"] = sha
        g.meta = meta
        arena.save_game(g)
        n += 1
    return n
