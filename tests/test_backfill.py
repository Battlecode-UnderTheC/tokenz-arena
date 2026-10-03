"""The metrics backfill reads each game's stored metrics once, not on every run."""

import threading
from types import SimpleNamespace

from arena import metrics, store
from arena.metrics import backfill


def arena_with(tmp_path, n):
    d = tmp_path / "metrics"
    d.mkdir()
    current = {"format": metrics.FORMAT, "versions": metrics.versions()}
    for i in range(n):
        store.write_json(d / f"{i}.json", current)
    games = [SimpleNamespace(id=i, replay=f"{i}.replay", lane="draft") for i in range(n + 1)]   # the last has none
    return SimpleNamespace(lock=threading.Lock(), games=games, cfg=SimpleNamespace(data_dir=tmp_path))


def test_current_games_are_read_once(tmp_path, monkeypatch):
    a = arena_with(tmp_path, 5)
    backfill._current.clear()
    backfill._versions = ()
    reads = []
    real = store.read_json
    monkeypatch.setattr(store, "read_json", lambda p, d=None: reads.append(p) or real(p, d))
    assert [g.id for g in backfill.todo(a, 10)] == [5]          # only the game with no metrics
    assert len(reads) == 5
    reads.clear()
    assert [g.id for g in backfill.todo(a, 10)] == [5]
    assert reads == []                                            # nothing reread


def test_a_new_metric_version_reads_them_again(tmp_path, monkeypatch):
    a = arena_with(tmp_path, 3)
    backfill._current.clear()
    backfill._versions = ()
    backfill.todo(a, 10)
    changed = {**metrics.versions(), "a_new_metric": 1}
    monkeypatch.setattr(metrics, "versions", lambda: changed)
    assert sorted(g.id for g in backfill.todo(a, 10)) == [0, 1, 2, 3]   # all outdated now
