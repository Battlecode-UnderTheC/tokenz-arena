"""The live game queue (arena/queue.py): same picks as the scheduler it replaced, incremental
state equal to a rebuild, atomic starts and results, live versions, and the website's fallback."""

import random
import threading
import time

from arena import queue as Q
from arena import scheduler, submit
from arena.state import Arena, Game, current_game, now
from tests.test_core import BOT, add, arena, make_zip  # noqa: F401  (fixture)


def reference_next_jobs(a, limit):
    """The scheduler before the queue (scanned every game on every call): the order to match."""
    taken = {(g.a, g.b, g.suite, g.map, g.seed) for g in a.games if current_game(g)}
    taken |= set(a.running)
    owed = []
    for s in a.subs.values():
        owed += [j for j in scheduler.owed_games(a, s) if j.key not in taken] \
            if s.status in Q.OWING else []
    if not owed:
        owed = [j for j in scheduler._holdout(a) if j.key not in taken]
    if not owed and a.cfg.get("play", "continuous", True):
        owed = scheduler._continuous(a, taken)
    busy = {}
    for r in a.running.values():
        busy[r["job"].owner] = busy.get(r["job"].owner, 0) + 1
    created = {sid: s.created for sid, s in a.subs.items()}
    out, seen = [], set()
    while owed and len(out) < limit:
        owed.sort(key=lambda j: (j.priority, busy.get(j.owner, 0), created.get(j.for_sub, ""), j.for_sub,
                                 j.map, j.seed, j.key))   # ties made explicit (were list order)
        j = owed.pop(0)
        if j.key in seen:
            continue
        seen.add(j.key)
        out.append(j)
        busy[j.owner] = busy.get(j.owner, 0) + 1
    return out


def play(a, job, winner="A"):
    g = a.add_game(Game(id=0, lane=job.lane, suite=job.suite, map=job.key[3], seed=job.seed, a=job.a,
                        b=job.b, winner=winner, rounds=500))
    Q.get(a).record(g, job)
    return g


def setup(a):
    p = add(a, "base")
    q = add(a, "other")
    r = add(a, "third")
    for s in (p, q, r):
        s.status = "rated"
    a.ratings = {p.id: {"elo": 1600}, q.id: {"elo": 1500}, r.id: {"elo": 1400}}
    d1 = add(a, "base", lane="draft", parent=p.id)
    s2, _ = submit.accept(a, blob=make_zip({**BOT, "y": "2"}), bot="other", owner="bob", lane="draft",
                          parent=q.id)
    (a.cfg.bots / s2.id).mkdir(parents=True)
    s2.status = "queued"
    return p, q, r, d1, s2


def test_same_picks_as_the_old_scheduler(arena):
    setup(arena)
    q = Q.get(arena)
    rng = random.Random(1)
    for step in range(40):
        want = reference_next_jobs(arena, 6)
        got = q.peek(6)
        assert [j.key for j in got] == [j.key for j in want], step
        if not got:
            break
        started = q.take(rng.randint(1, 3))
        for j in started[: rng.randint(0, len(started))]:
            play(arena, j, rng.choice(["A", "B"]))
        q.sync()


def test_incremental_state_equals_a_rebuild(arena):
    p, _, _, d1, s2 = setup(arena)
    q = Q.get(arena)
    rng = random.Random(2)
    for step in range(30):
        jobs = q.take(rng.randint(1, 4), "local" if step % 2 else "w1",
                      None if step % 2 else (lambda j: {"lease": "x"}))
        for j in jobs:
            r = rng.random()
            if r < 0.6:
                play(arena, j)
            elif r < 0.8:
                q.release(j, arena.running[j.key]["worker"])
        if step == 10:                                  # a status change outside the queue
            submit.make_current(arena, d1)
        q.sync()
    fresh = Q.GameQueue(arena)
    assert q.left == fresh.left and q.played == fresh.played
    assert {sid: set(j) for sid, j in q.jobs.items()} == {sid: set(j) for sid, j in fresh.jobs.items()}


def test_take_is_atomic_and_holdout_stays_local(arena):
    setup(arena)
    q = Q.get(arena)
    got: list = []
    threads = [threading.Thread(target=lambda: got.extend(q.take(5, "w", lambda j: {"lease": "x"})))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys = [j.key for j in got]
    assert len(keys) == len(set(keys)) and all(j.suite != "holdout" for j in got)


def test_versions_and_waiting(arena):
    setup(arena)
    q = Q.get(arena)
    v = q.version
    threading.Timer(0.2, lambda: q.take(1)).start()
    t = time.time()
    assert q.wait(v, 5) > v and time.time() - t < 3            # woke on the change
    assert q.wait(q.version, 0.1) == q.version                 # no change: times out
    view = q.view()
    assert view["running"] and view["totals"]["left"] == sum(w["left"] for w in view["waiting"].values())


def test_record_once_and_settle(arena):
    from arena.daemon import Daemon
    p, q_, r, d1, s2 = setup(arena)
    d = Daemon(arena)
    q = Q.get(arena)
    for j in list(q.jobs[d1.id].values()):
        arena.running[j.key] = {"job": j, "started": now(), "worker": "local"}
        res = {"winner": "A", "rounds": 500, "reason": "longest"}
        assert d.record_result(j, res, "local") is True
        assert d.record_result(j, res, "local") is False        # a duplicate result is dropped
    assert arena.subs[d1.id].status == "done" and not q.left.get(d1.id)
    d.procs.shutdown(cancel_futures=True)


def test_website_reads_the_snapshot(arena, tmp_path):
    from fastapi.testclient import TestClient

    from arena.api.http import create_app
    setup(arena)
    Q.get(arena).take(2)
    Q.get(arena).snapshot()
    web = Arena(arena.cfg, readonly=True)
    body = TestClient(create_app(web)).get("/api/v1/queue").json()
    assert body["live"] is False and len(body["running"]) == 2 and body["waiting"]
