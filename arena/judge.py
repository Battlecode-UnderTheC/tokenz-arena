"""One game, run in a worker process (the engine's process pool): the judge itself is a
separate `unswbc` process, but its ~100k-line log is parsed here too, so none of that work
holds the engine's GIL. Returns only the small result."""

from __future__ import annotations

from pathlib import Path

from arena import runner, stats


def run_game(map_path: str, bot_a: str, bot_b: str, seed: int, replay_dir: str, work_root: str,
             timeout: float) -> dict:
    try:
        r = runner.play(Path(map_path), Path(bot_a), Path(bot_b), seed, Path(replay_dir),
                        work_root=Path(work_root), timeout=timeout)
    except runner.JudgeError as e:
        return {"error": str(e)[:2000]}
    out = {"winner": r.winner, "rounds": r.rounds, "reason": r.reason,
           "seconds": round(r.seconds, 1), "stats": stats.extract(r),
           "replay": str(r.replay) if r.replay else None}
    if r.replay:                       # in-game metrics, computed where the game was played
        try:
            from arena import metrics
            out["metrics"] = metrics.compute(Path(r.replay).read_bytes())
        except Exception as e:         # never lose a game over a metric
            out["metrics_error"] = f"{type(e).__name__}: {e}"[:300]
    return out
