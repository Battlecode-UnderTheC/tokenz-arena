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


def test_pearl_rate_is_pearls_over_the_last_50_rounds():
    from pathlib import Path
    from arena import metrics
    d = metrics.compute((Path(__file__).parent / "data" / "colosseum-seed1.replay").read_bytes())
    rounds, eaten, rate = d["rounds"], d["team"]["pearls_eaten"]["A"], d["team"]["pearl_rate"]["A"]
    i, j = rounds.index(68), rounds.index(10)          # 68 - 50 = 18: nothing eaten by then
    assert eaten[j] == 0 and rate[i] == round(eaten[i] / 50, 4)
    k = rounds.index(40)                               # fewer than 50 rounds: per round so far
    assert rate[k] == round(eaten[k] / 40, 4)


def test_exploration_metrics_grow_and_views_wrap():
    from pathlib import Path
    from arena import metrics
    from arena.metrics import state as S
    g = S.GameMap("MAP 10 8\n")
    v = metrics.view(g, (0, 0))
    assert len(set(v)) == 49 and (9, 7) in v and (3, 3) in v and (4, 0) not in v   # wraps, radius 3
    m = metrics.compute((Path(__file__).parent / "data" / "colosseum-seed1.replay").read_bytes())
    for k in ("tiles_visited", "tiles_seen"):
        for t in "AB":
            s = m["team"][k][t]
            assert s == sorted(s) and 0 < s[0] and s[-1] <= 1          # only ever grows
        assert all(m["team"]["tiles_seen"][t][i] >= m["team"]["tiles_visited"][t][i]
                   for t in "AB" for i in range(len(m["rounds"])))     # a head sees where it stands


def test_native_metrics_match_python_when_built():
    """arena-metrics (native/metrics, built with native/metrics/build.sh) gives exactly the Python
    metrics, and lists the same metrics with the same descriptions. Skipped where it isn't built."""
    import pytest
    from pathlib import Path
    from arena import metrics
    from arena.metrics import native
    if native.binary() is None:
        pytest.skip("arena-metrics isn't built here")
    blob = (Path(__file__).parent / "data" / "colosseum-seed1.replay").read_bytes()
    assert native.compute(blob) == metrics.compute_python(blob)
    assert list(native.describe()) == metrics.describe()
    assert native.usable()
