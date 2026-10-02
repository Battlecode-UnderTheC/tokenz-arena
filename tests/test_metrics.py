"""In-game metrics: the registry, computing from a real replay, staleness, and the summaries."""

from pathlib import Path

from arena import metrics, store
from arena.metrics import views

REPLAY = Path(__file__).parent / "data" / "colosseum-seed1.replay"


def test_compute_from_a_real_replay():
    m = metrics.compute(REPLAY.read_bytes())
    assert m["rounds"][0] == 1 and m["rounds"][-1] == 68        # the game ends in round 68
    for name in ("dragons", "board_control", "longest", "queen_alive", "pearls_eaten"):
        assert len(m["team"][name]["A"]) == len(m["rounds"])
    assert m["team"]["dragons"]["B"][-1] == 0 and m["result"]["winner"] == "A"
    ev = m["game"]["eval"]
    assert all(0 <= p <= 1 for p in ev) and ev[-1] == 1.0       # B eliminated: A has won
    assert not m["errors"]


def test_a_new_metric_is_picked_up_and_old_files_are_outdated():
    stored = metrics.compute(REPLAY.read_bytes())
    assert not metrics.outdated(stored)

    @metric_once("test_heads", "Heads", "test")
    def heads(ctx, t):
        return len(ctx.heads(t))
    try:
        assert metrics.outdated(stored)                         # missing the new metric
        again = metrics.compute(REPLAY.read_bytes())
        assert again["team"]["test_heads"]["A"][0] >= 1
    finally:
        metrics.REGISTRY.pop("test_heads", None)


def metric_once(*a, **k):
    return metrics.metric(*a, **k)


def test_a_failing_metric_does_not_break_the_rest():
    @metrics.metric("test_broken", "Broken", "raises")
    def broken(ctx, t):
        raise ValueError("nope")
    try:
        m = metrics.compute(REPLAY.read_bytes())
        assert "test_broken" in m["errors"] and m["team"]["dragons"]["A"]
    finally:
        metrics.REGISTRY.pop("test_broken", None)


def test_submission_summary(tmp_path):
    from arena import config
    from arena.state import Arena, Game
    a = Arena(config.Config(raw={"maps": {"worldgen": False}}, data_dir=tmp_path / "d"))
    g = a.add_game(Game(id=0, lane="draft", suite="official", map="Colosseum", seed=1, a="x-1",
                        b="y-1", winner="A", rounds=68))
    store.write_json(a.cfg.data_dir / "metrics" / f"{g.id}.json", metrics.compute(REPLAY.read_bytes()))
    d = views.submission(a, "y-1")
    assert d["games"] == 1
    assert d["metrics"]["dragons"]["at"]["end"] == {"own": 0, "opponents": d["metrics"]["dragons"]["at"]["end"]["opponents"], "games": 1}
    assert d["metrics"]["eval"]["at"]["end"]["own"] == 0          # y-1 was B, and lost
