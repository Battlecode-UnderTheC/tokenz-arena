"""Checks arena-metrics (C++) against the Python metrics, the reference, on replay files.

    python native/metrics/parity.py [--binary PATH] [-j N] REPLAY...

Every value of every metric must be equal (after both round to 4 places), with the same
rounds, queen deaths, map and result; it prints the first differences per metric and the time
each side took. The C++ side runs as one batch over all files on -j threads."""
import argparse, json, os, subprocess, sys, tempfile, time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def python_one(path):
    from arena import metrics
    try:
        return metrics.compute_python(Path(path).read_bytes())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def diff(a, b, where=""):
    """The first places where a and b differ (ints and floats compare by value)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append(f"{where}.{k}: only in {'python' if k in a else 'native'}")
            else:
                out += diff(a[k], b[k], f"{where}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{where}: length {len(a)} vs {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{where}[{i}]")
            if out:
                return out
        return out
    return [] if a == b else [f"{where}: python {a!r} vs native {b!r}"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=str(ROOT / "native/metrics/build/arena-metrics"))
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("replays", nargs="+")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        pairs = [(r, os.path.join(tmp, f"{i}.json")) for i, r in enumerate(a.replays)]
        t = time.time()
        subprocess.run([a.binary, "-j", str(a.j)], input="".join(f"{r}\t{o}\n" for r, o in pairs),
                       text=True, check=True, stdout=subprocess.DEVNULL)
        tn = time.time() - t
        native = [json.load(open(o)) for _, o in pairs]
    t = time.time()
    with ProcessPoolExecutor(a.j) as pool:
        py = list(pool.map(python_one, a.replays, chunksize=2))
    tp = time.time() - t
    bad, kinds = 0, Counter()
    for r, p, n in zip(a.replays, py, native):
        d = diff(p, n)
        if d:
            bad += 1
            for x in d:
                kinds[x.split("[")[0]] += 1
            if bad <= 5:
                print(f"DIFF {r}:", *d[:4], sep="\n  ")
    print(f"{len(a.replays)} replays: identical {len(a.replays) - bad}/{len(a.replays)}; "
          f"native {tn:.2f}s, python {tp:.1f}s on {a.j} processes ({tp / tn:.0f}x)")
    if kinds:
        print("differences by field:", dict(kinds.most_common(12)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
