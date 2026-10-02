"""Replays stored official arena games with both judges and compares them.

For each game: the official judge (`unswbc run ... --sandbox --seed S -v -o ...`, exactly as
arena/runner.py play() calls it) and cppjudge (judge/unswbc-cpp with the same arguments) play
it again; each replay is decoded with arena/metrics/state.py load_bytes and compared with the
stored one (map, seed, format, result, events; the bot paths in the header may differ). The
two -v logs are compared too, minus the lines that carry wall-clock times. CPU seconds
(user + sys of the judge process and its children) and wall seconds are recorded for both.

Runs inside an image with the arena code at /app, unswbc, and cppjudge at /opt/cppjudge
(judge/parity.Dockerfile), with the arena data mounted read-only at /data:

    python judge/parity.py -n 60 -j 4 --out /work/parity.jsonl
    python judge/parity.py --ids 27840 --out /work/one.jsonl
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import multiprocessing
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/app")
from arena.metrics import state as S  # noqa: E402

DATA = Path(os.environ.get("PARITY_DATA", "/data"))
WRAPPER = os.environ.get("PARITY_CPP", "/opt/cppjudge/unswbc-cpp")
KEEP = os.environ.get("PARITY_KEEP_LOGS")   # a directory to keep both judges' logs in
TIMING = re.compile(r"^running round |wins after \d+ rounds|^draw after |^wrote replay: ")


def map_path(suite: str, name: str) -> str | None:
    cands = {"official": [f"/app/maps/official/{name}.map"],
             "tournament": [f"/app/maps/tournament/{name}.map"],
             "server": [f"{DATA}/maps/server/{name}.map"],
             "custom": [f"{DATA}/maps/custom/{name}.map"]}.get(suite)
    if suite == "rotating":
        cands = glob.glob(f"{DATA}/maps/rotating/*/{name}.map")
    for c in cands or []:
        if os.path.exists(c):
            return c
    return None


def candidates(limit: int = 8000, ids=None):
    out = []
    files = ([f"{DATA}/games/{i}.json" for i in ids] if ids else
             sorted(glob.glob(f"{DATA}/games/*.json"), key=lambda p: -int(Path(p).stem))[:limit])
    for f in files:
        g = json.load(open(f))
        m = g.get("meta") or {}
        if g.get("error") or not g.get("replay") or g.get("suite") == "holdout":
            continue
        if m.get("engine") != "1.2.7" or not m.get("map_sha"):
            continue
        mp = map_path(g["suite"], g["map"])
        rp = DATA / "replays" / g["replay"]
        if not mp or not rp.exists():
            continue
        if hashlib.sha256(open(mp, "rb").read()).hexdigest() != m["map_sha"]:
            continue
        if not all((DATA / "bots" / g[t]).is_dir() for t in "ab"):
            continue
        out.append((g, mp))
    return out


def decoded(blob: bytes) -> dict:
    d = S.load_bytes(blob)
    return {k: v for k, v in d.items() if k not in ("botA", "botB")}


def timed(cmd: list[str], cwd: str) -> tuple[int, str, float, float]:
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    start = time.monotonic()
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=env)
        _, status, usage = os.wait4(proc.pid, 0)
        wall = time.monotonic() - start
        log.seek(0)
        text = log.read().decode("utf-8", "replace")
    return os.waitstatus_to_exitcode(status), text, usage.ru_utime + usage.ru_stime, wall


def bot_dir(name: str) -> Path:
    return Path(name) if name.startswith("/") else DATA / "bots" / name


def play(g: dict, mp: str, judge: str, work: Path) -> dict:
    """runner.play(): fresh copies of both bots at <work>/A/<id> and <work>/B/<id>."""
    a = work / "A" / Path(g["a"]).name
    b = work / "B" / Path(g["b"]).name
    for src, dst in ((g["a"], a), (g["b"], b)):
        if not dst.exists():
            shutil.copytree(bot_dir(src), dst)
    out = work / f"replay-{judge}"
    exe = ["unswbc"] if judge == "official" else [sys.executable, WRAPPER]
    cmd = [*exe, "run", mp, str(a), str(b), "--sandbox", "--seed", str(g["seed"]), "-v", "-o", str(out)]
    code, log, cpu, wall = timed(cmd, str(work))
    replays = sorted(out.glob("*.replay"))
    return {"code": code, "log": log, "cpu": cpu, "wall": wall,
            "replay": replays[0].read_bytes() if replays else None}


def one(job) -> dict:
    g, mp, judges = job
    ref = decoded((DATA / "replays" / g["replay"]).read_bytes()) if g.get("replay") else None
    row = {"id": g["id"], "map": g["map"], "suite": g["suite"], "a": g["a"], "b": g["b"],
           "rounds": g["rounds"], "winner": g["winner"], "arena_seconds": g.get("seconds")}
    with tempfile.TemporaryDirectory(prefix="parity-") as w:
        work = Path(w)
        runs = {}
        for judge in judges:
            r = play(g, mp, judge, work)
            runs[judge] = r
            if KEEP:
                Path(KEEP).mkdir(parents=True, exist_ok=True)
                Path(KEEP, f"{g['id']}-{judge}.log").write_text(r["log"])
            if ref is None and r["replay"] is not None:
                ref = decoded(r["replay"])     # an ad-hoc game: the official judge's is the reference
            same = r["replay"] is not None and decoded(r["replay"]) == ref
            row[judge] = {"same": same, "code": r["code"], "cpu": round(r["cpu"], 3),
                          "wall": round(r["wall"], 3)}
            if not same:
                row[judge]["tail"] = r["log"].strip().splitlines()[-4:]
        if "official" in runs and "cpp" in runs:
            strip = lambda t: [l for l in t.splitlines() if not TIMING.search(l)]
            lo, lc = strip(runs["official"]["log"]), strip(runs["cpp"]["log"])
            row["log_same"] = lo == lc
            if lo != lc:
                diff = next((i for i, (x, y) in enumerate(zip(lo, lc)) if x != y), min(len(lo), len(lc)))
                row["log_diff"] = {"line": diff, "official": lo[diff:diff + 2], "cpp": lc[diff:diff + 2],
                                   "lines": [len(lo), len(lc)]}
            row["fallback"] = "loaded wasm vs wasm" not in runs["cpp"]["log"]
    return row


def prewarm(games) -> None:
    """Builds every bot once and compiles its metered wasm for both judges, so no game's
    timing includes a one-off build (the official judge's clang takes ~60 s a bot)."""
    from unswbc import clangtool
    from unswbc.sandbox import _metered, compile_once
    import unswbc
    subprocess.run([str(Path(WRAPPER).with_name("cppjudge")), "warm-engine",
                    str(Path(unswbc.__file__).with_name("unswbc_engine.wasm"))], check=True)
    seen = set()
    with tempfile.TemporaryDirectory(prefix="prewarm-") as w:
        for g, _ in games:
            for bot in (g["a"], g["b"]):
                if bot in seen:
                    continue
                seen.add(bot)
                dst = Path(w) / Path(bot).name
                shutil.copytree(bot_dir(bot), dst)
                if not clangtool.sources(dst):
                    continue
                t = time.monotonic()
                wasm = Path(clangtool.build(dst))
                metered = Path(_metered(wasm))
                compile_once(metered)
                subprocess.run([str(Path(WRAPPER).with_name("cppjudge")), "imports", str(metered)],
                               stdout=subprocess.DEVNULL)
                print(f"prewarmed {bot} ({time.monotonic() - t:.0f}s)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=12)
    ap.add_argument("-j", type=int, default=2)
    ap.add_argument("--ids", default="")
    ap.add_argument("--judges", default="official,cpp")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="/work/parity.jsonl")
    ap.add_argument("--adhoc", default="", help="BOT_A,BOT_B,MAP[,MAP...]: new games, official vs cpp")
    ap.add_argument("--seeds", default="1,2,3")
    args = ap.parse_args()
    judges = args.judges.split(",")
    if args.adhoc:
        bot_a, bot_b, *maps = args.adhoc.split(",")
        games = pick = [({"id": f"{Path(m).stem}-{seed}", "map": Path(m).stem, "suite": "adhoc", "a": bot_a,
                          "b": bot_b, "seed": int(seed), "rounds": None, "winner": None}, m)
                        for m in maps for seed in args.seeds.split(",")]
    elif args.ids:
        games = pick = candidates(ids=[int(x) for x in args.ids.split(",")])
    else:
        games = candidates()
        rnd = random.Random(args.seed)
        long_ = [x for x in games if (x[0]["rounds"] or 0) >= 400]
        short = [x for x in games if (x[0]["rounds"] or 0) < 400]
        # spread over maps and bot pairs: at most 2 games per map in each half
        def spread(pool, k):
            rnd.shuffle(pool)
            per, out = {}, []
            for x in pool:
                if per.get(x[0]["map"], 0) < 2 and len(out) < k:
                    per[x[0]["map"]] = per.get(x[0]["map"], 0) + 1
                    out.append(x)
            return out
        pick = spread(long_, args.n // 2) + spread(short, args.n - args.n // 2)
    print(f"{len(games)} candidate games; testing {len(pick)} with {judges}", flush=True)
    prewarm(pick)
    jobs = [(g, mp, judges) for g, mp in pick]
    totals = {j: [0, 0.0, 0.0] for j in judges}
    with open(args.out, "a") as out, multiprocessing.Pool(args.j) as pool:
        for row in pool.imap_unordered(one, jobs):
            out.write(json.dumps(row) + "\n")
            out.flush()
            line = f"game {row['id']} {row['map']} r{row['rounds']} {row['a']} vs {row['b']}"
            for j in judges:
                r = row[j]
                totals[j][0] += r["same"]
                totals[j][1] += r["cpu"]
                totals[j][2] += r["wall"]
                line += f" | {j} {'SAME' if r['same'] else 'DIFF'} cpu {r['cpu']:.1f}s wall {r['wall']:.1f}s"
            if "log_same" in row:
                line += f" | log {'same' if row['log_same'] else 'DIFF'}"
            print(line, flush=True)
    n = len(pick)
    for j, (same, cpu, wall) in totals.items():
        print(f"{j}: identical {same}/{n}  cpu {cpu:.1f}s  wall {wall:.1f}s")
    if all(j in totals for j in ("official", "cpp")) and totals["cpp"][1]:
        print(f"cpu ratio official/cpp: {totals['official'][1] / totals['cpp'][1]:.2f}x")


if __name__ == "__main__":
    main()
