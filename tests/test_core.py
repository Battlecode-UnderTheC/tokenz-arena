"""Scheduler, rating and submission checks on a fake arena (no judge games)."""

import io
import zipfile

import pytest

from arena import config, rating, scheduler, submit
from arena.state import Arena, Game


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


BOT = {"bot.toml": '[project]\nlanguage = "py"\n', "main.py": "print('hi')\n"}


@pytest.fixture
def arena(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    for m in ("one", "two"):
        (maps / f"{m}.map").write_text("MAP 4 4\n")
    cfg = config.Config(raw={"maps": {"official": str(maps)}}, data_dir=tmp_path / "data")
    return Arena(cfg)


def add(arena, bot, lane="ranked", files=BOT, parent=None):
    s, _ = submit.accept(arena, blob=make_zip({**files, "x": bot + lane + str(parent)}), bot=bot,
                         owner="alice", lane=lane, parent=parent)
    (arena.cfg.bots / s.id).mkdir(parents=True)
    if lane == "ranked":
        submit.make_current(arena, s)
    else:
        s.status = "queued"
    return s


def test_rejects_bad_zips(arena):
    with pytest.raises(submit.Rejected):
        submit.accept(arena, blob=b"nope", bot="x", owner="a")
    with pytest.raises(submit.Rejected):
        submit.accept(arena, blob=make_zip({"main.py": ""}), bot="x", owner="a")
    with pytest.raises(submit.Rejected):
        submit.accept(arena, blob=make_zip({"../evil": "", "bot.toml": ""}), bot="x", owner="a")


def test_duplicate_returns_original(arena):
    a, new = submit.accept(arena, blob=make_zip(BOT), bot="x", owner="a")
    b, again = submit.accept(arena, blob=make_zip(BOT), bot="y", owner="b")
    assert new and not again and a.id == b.id


def test_draft_plays_parent_and_top_both_seats(arena):
    p = add(arena, "base")
    q = add(arena, "other")
    d = add(arena, "base", lane="draft", parent=p.id)
    jobs = scheduler.owed_for(arena, d)
    assert {(j.a, j.b) for j in jobs} == {(d.id, p.id), (p.id, d.id), (d.id, q.id), (q.id, d.id)}
    assert len(jobs) == 2 * 2 * 2   # 2 opponents x 2 maps x 2 seats


def test_settle_and_rating(arena):
    p = add(arena, "base")
    q = add(arena, "other")
    for j in scheduler.owed_for(arena, q):
        arena.add_game(Game(id=0, lane=j.lane, suite=j.suite, map=j.key[3], seed=j.seed, a=j.a,
                            b=j.b, winner="A" if j.a == q.id else "B", rounds=500))
    scheduler.settle(arena)
    assert q.status == "rated"
    r = rating.fit(arena)
    assert r[q.id]["elo"] > r[p.id]["elo"]


def test_identity_from_tailscale_headers():
    from arena import who
    h = {"tailscale-user-login": "alice@github", "authorization": "Bearer worker-3"}
    assert who.identify(h, "127.0.0.1").owner == "alice@github"
    who.TRUST_HEADERS = False
    assert who.identify(h, "100.1.2.3").owner != "alice@github"   # a forged header from outside
    who.TRUST_HEADERS = True
    try:
        c = who.identify(h, "100.1.2.3")
        assert (c.owner, c.agent) == ("alice@github", "worker-3")
    finally:
        who.TRUST_HEADERS = False
