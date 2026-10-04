"""The vast.ai scaler against a fake Vast API and fake queue states: how much it rents, caps,
idle returns, dry run, reconciliation, and the engine honouring its per-instance tokens."""

import pytest

from arena import config, store
from arena.scaler import LABEL, Scaler

OFFERS = [
    {"id": 1, "dph_total": 0.64, "cpu_cores_effective": 256, "cpu_ram": 252 * 1024, "cpu_name": "EPYC 7742", "geolocation": "TW"},
    {"id": 2, "dph_total": 0.11, "cpu_cores_effective": 32, "cpu_ram": 31 * 1024, "cpu_name": "EPYC 7702", "geolocation": "NO"},
    {"id": 3, "dph_total": 0.09, "cpu_cores_effective": 36, "cpu_ram": 31 * 1024, "cpu_name": "E5-2686", "geolocation": "VN"},
    {"id": 4, "dph_total": 0.90, "cpu_cores_effective": 32, "cpu_ram": 64 * 1024, "cpu_name": "pricey", "geolocation": "US"},
]


class FakeVast:
    def __init__(self):
        self.live, self.created, self.destroyed, self.next = {}, [], [], 100

    def offers(self, q):
        return [dict(o) for o in OFFERS]

    def instances(self):
        return list(self.live.values())

    def create(self, offer_id, body):
        self.next += 1
        self.created.append((offer_id, body))
        self.live[self.next] = {"id": self.next, "label": body["label"], "actual_status": "running"}
        return {"success": True, "new_contract": self.next}

    def destroy(self, iid):
        self.destroyed.append(iid)
        self.live.pop(iid, None)
        return {"success": True}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def queue(left, local=20, remote=()):
    return {"totals": {"left": left}, "running": [], "avg_game_s": 30,
            "slots": {"local": local}, "remote": list(remote)}


@pytest.fixture
def make(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "worker-ts-authkey").write_text("tskey-auth-x\n")
    (secrets / "registry-login").write_text("svc s3cret\n")

    def make(**over):
        raw = {"scaler": {"dry_run": False, "secrets_dir": str(secrets), "image": "registry.syeyoung.dev/tokenz-arena:latest",
                          "interruptible": False, "min_threads": 32, **over}}
        cfg = config.Config(raw=raw, data_dir=tmp_path / "data")
        clock, vast = Clock(), FakeVast()
        s = Scaler(cfg, vast=vast, clock=clock, engine="http://unused")
        return s, vast, clock
    return make


def test_rents_for_the_backlog_within_caps(make):
    s, vast, clock = make(target_minutes=30)
    s.queue = lambda: queue(left=3000)                 # 3000 x 30 s / 30 min = 50 slots; we own 20
    p = s.tick()
    assert p["needed_slots"] == 50 and p["want_rented"] == 30
    (offer, body), = vast.created
    assert offer == 1                                  # cheapest per slot: the 256-thread box
    assert body["image"].startswith("registry.syeyoung.dev/") and body["args"] == ["worker"]
    assert body["label"].startswith(LABEL + ":") and body["env"]["TS_AUTHKEY"] == "tskey-auth-x"
    assert body["image_login"] == "-u svc -p s3cret registry.syeyoung.dev"
    name = body["env"]["WORKER_NAME"]
    tokens = store.read_json(s.cfg.data_dir / "worker-tokens.json")
    assert tokens[name]["token"] == body["env"]["ARENA_WORKER_TOKEN"]
    # never past the hourly cap: offer 4 ($0.90/h) would break $1/h with what's rented
    for _ in range(6):
        clock.t += 60
        s.tick()
    assert s.per_hour() <= 1.0 and all(o != 4 for o, _ in vast.created)


def test_dry_run_rents_nothing(make):
    s, vast, _ = make(dry_run=True)
    s.queue = lambda: queue(left=3000)
    p = s.tick()
    assert p["decision"].startswith("[dry run] need") and not vast.created


def test_returns_idle_machines_and_revokes_their_tokens(make):
    s, vast, clock = make(min_minutes=20, idle_minutes=10)
    s.queue = lambda: queue(left=3000)
    s.tick()
    (_, body), = vast.created
    name = body["env"]["WORKER_NAME"]
    s.queue = lambda: queue(left=0, remote=[{"name": name, "slots": 16, "running": 0}])
    clock.t += 5 * 60
    s.tick()
    assert name in s.state["instances"]                 # too young to return
    clock.t += 30 * 60
    s.tick()
    assert name not in s.state["instances"] and vast.destroyed
    assert store.read_json(s.cfg.data_dir / "worker-tokens.json")[name]["revoked"] is True


def test_daily_cap_returns_everything(make):
    s, vast, clock = make(max_per_day=0.05)
    s.queue = lambda: queue(left=3000)
    s.tick()
    assert s.state["instances"]
    clock.t += 3600                                      # an hour at $0.09-0.11/h > $0.05
    p = s.tick()
    assert p["decision"] == "daily cap reached" and not s.state["instances"] and vast.destroyed


def test_reconcile_destroys_strangers_and_forgets_the_vanished(make):
    s, vast, clock = make()
    vast.live[7] = {"id": 7, "label": f"{LABEL}:vast-stray", "actual_status": "running"}
    vast.live[8] = {"id": 8, "label": "someone-else", "actual_status": "running"}   # not ours: left alone
    s.queue = lambda: queue(left=0)
    s.tick()
    assert 7 in vast.destroyed and 8 not in vast.destroyed
    s.state["instances"]["vast-gone"] = {"id": 9, "name": "vast-gone", "slots": 16, "dph": 0.1,
                                         "created_t": clock.t, "busy_t": clock.t}
    clock.t += 60
    s.tick()
    assert "vast-gone" not in s.state["instances"]


def test_engine_takes_scaler_tokens_and_refuses_revoked(tmp_path):
    from arena.state import Arena
    from arena.workers import Refused, Registry, tokens_file
    cfg = config.Config(raw={"workers": {"tokens": {"team": "static-token"}}}, data_dir=tmp_path / "data")
    reg = Registry(Arena(cfg), daemon=None)
    with tokens_file(cfg) as d:
        d["vast-abc"] = {"token": "dyn-token"}
    assert reg.token_name("dyn-token") == "vast-abc" and reg.token_name("static-token") == "team"
    with tokens_file(cfg) as d:
        d["vast-abc"]["revoked"] = True
    with pytest.raises(Refused):
        reg.token_name("dyn-token")


def test_interruptible_bids_and_replaces_paused_machines(make):
    s, vast, clock = make(interruptible=True, bid_margin=1.2, paused_minutes=10)
    s.queue = lambda: queue(left=3000)
    s.tick()
    (offer, body), = vast.created
    assert body["price"] == round(0.64 * 1.2, 4)                    # the cheapest per slot, bid +20%
    name = body["env"]["WORKER_NAME"]
    s.queue = lambda: queue(left=3000, remote=[{"name": name, "slots": 18, "running": 3}])
    clock.t += 60
    s.tick()                                                         # joined
    iid = s.state["instances"][name]["id"]
    vast.live[iid]["actual_status"] = "exited"                       # outbid
    s.queue = lambda: queue(left=3000)
    clock.t += 60
    s.tick()
    assert s.state["instances"][name].get("paused_t")
    clock.t += 11 * 60
    s.tick()
    assert name not in s.state["instances"] and iid in vast.destroyed


def test_outbid_while_loading_is_returned_at_once_and_is_no_boot_failure(make):
    """Vast keeps actual_status "loading" for a machine outbid before it booted; its intended and
    current state say "stopped". It's outbid, not a boot failure, and goes on the next tick."""
    s, vast, clock = make(interruptible=True, paused_minutes=0)
    s.queue = lambda: queue(left=3000)
    s.tick()
    (offer, body), = vast.created
    name = body["env"]["WORKER_NAME"]
    iid = s.state["instances"][name]["id"]
    vast.live[iid].update(actual_status="loading", intended_status="stopped", cur_state="stopped")
    clock.t += 60
    s.tick()
    assert name not in s.state["instances"] and iid in vast.destroyed
    assert s.state["boot_failures"] == 0


def test_engine_down_returns_everything(make):
    s, vast, clock = make(engine_down_minutes=10)
    s.queue = lambda: queue(left=3000)
    s.tick()
    assert s.state["instances"]

    def down():
        raise OSError("connection refused")
    s.queue = down
    clock.t += 5 * 60
    assert s.tick()["decision"] == "engine unreachable" and s.state["instances"]   # not yet
    clock.t += 6 * 60
    s.tick()
    assert not s.state["instances"] and vast.destroyed


def test_boot_failures_halt_renting_and_rents_are_rate_limited(make):
    s, vast, clock = make(boot_minutes=15, max_boot_failures=2, max_rents_per_hour=3, max_instances=6)
    s.queue = lambda: queue(left=3000)                 # machines never show up as workers
    for _ in range(40):
        clock.t += 60
        s.tick()
    assert len(vast.created) <= 3 + 1                  # at most 3 an hour (40 min here, plus the first)
    assert s.state.get("halted") and "never joined" in s.state["halted"]
    n = len(vast.created)
    clock.t += 2 * 3600
    s.tick()
    assert len(vast.created) == n                      # halted: no more rentals


def test_spend_counts_while_the_scaler_was_down(make, tmp_path):
    s, vast, clock = make()
    s.queue = lambda: queue(left=3000)
    s.tick()
    rate = s.per_hour()
    s.save()
    clock.t += 2 * 3600                                # the scaler was down for two hours
    s2 = Scaler(s.cfg, vast=vast, clock=clock, engine="http://unused")
    s2.queue = lambda: queue(left=3000)
    s2.tick()
    assert s2.spent_today() == pytest.approx(rate * 2, rel=0.01)


def test_a_rental_must_fit_under_todays_cap_for_its_minimum_time(make):
    s, vast, clock = make(max_per_day=0.02, min_minutes=20)      # 20 min of any offer > $0.02
    s.queue = lambda: queue(left=3000)
    p = s.tick()
    assert not vast.created and "no offer fits" in p["decision"]


def test_prefers_big_machines(make):
    s, vast, _ = make(min_threads=64)
    s.queue = lambda: queue(left=3000)
    s.tick()
    (offer, _), = vast.created
    assert offer == 1                                  # the 256-thread box, not a stack of small ones


def test_rents_only_above_500_games(make):
    s, vast, clock = make(target_minutes=1)            # tiny target: any backlog "needs" more slots
    s.queue = lambda: queue(left=500)
    p = s.tick()
    assert not vast.created and "renting starts above 500" in p["decision"]
    s.queue = lambda: queue(left=501)
    clock.t += 60
    s.tick()
    assert vast.created


def test_machines_that_vanish_before_joining_halt_renting(make):
    s, vast, clock = make(max_boot_failures=2, max_rents_per_hour=10)
    s.queue = lambda: queue(left=3000)
    for _ in range(4):                                 # each machine dies right after it's rented
        s.tick()
        vast.live.clear()
        clock.t += 60
    s.tick()
    assert len(vast.created) == 2 and "halted" in s.state and "before joining" in s.state["halted"]
