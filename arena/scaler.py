"""The scaler, `arena scaler` (docs/SCALER.md): rents vast.ai machines as remote workers when the
queue is long and returns them when it's short. Its own process (compose service `scaler`).

Every [scaler] tick_seconds (60) it reads the engine's live queue and decides how many worker
slots the backlog needs:

    needed  = ceil(games left x average game length / [scaler] target_minutes)
    rented  = needed - our own slots (the engine's judges and our machines' workers)

It starts the cheapest acceptable offers (verified hosts, enough threads, RAM and disk,
reliable, cheap bandwidth; ranked by $ per slot) and destroys an instance when the backlog no
longer needs it and it has had nothing to play for [scaler] idle_minutes (but never before
[scaler] min_minutes, so it doesn't flap). It never spends past [scaler] max_per_hour or
max_per_day: at the daily cap it returns every machine.

Each instance runs the worker image (`worker`) with its own worker token (worker-tokens.json,
revoked when it's destroyed) and the workers' Tailscale key. Instances are labelled
`tokenz-worker`; on start the scaler adopts the ones it knows and destroys any it doesn't, so a
crash never leaves machines billing.

With [scaler] dry_run (the default) it only reports what it would do: it reads real offers
(Vast's offer search needs no key) but rents nothing.

State and history: data_dir/scaler/state.json (shown on the Queue page and /api/v1/scaler).
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena import store
from arena.workers import tokens_file

log = logging.getLogger("arena.scaler")

VAST = "https://console.vast.ai"
LABEL = "tokenz-worker"
UA = "tokenz-arena scaler"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VastError(Exception):
    pass


class Vast:
    """The few Vast.ai API calls the scaler needs, as the official `vastai` CLI (1.8) makes them.
    The key is read from a file, held here only, and never logged."""

    def __init__(self, key: str | None, timeout: float = 30):
        self._key = key
        self.timeout = timeout

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None,
              auth: bool = True):
        if not path.startswith("/api/v"):
            path = "/api/v0" + path
        qs = "&".join(f"{k}={urllib.parse.quote_plus(v if isinstance(v, str) else json.dumps(v))}"
                      for k, v in (query or {}).items())
        url = f"{VAST}{path}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", UA)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            if not self._key:
                raise VastError("no Vast API key ([scaler] key_file)")
            req.add_header("Authorization", f"Bearer {self._key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = (e.read() or b"")[:300].decode("utf-8", "replace")
            raise VastError(f"{method} {path}: HTTP {e.code} {detail}") from None
        except (urllib.error.URLError, OSError) as e:
            raise VastError(f"{method} {path}: {getattr(e, 'reason', e)}") from None
        return json.loads(raw) if raw else {}

    def offers(self, q: dict) -> list[dict]:
        d = self._call("GET", "/bundles/", {"q": json.dumps(q)}, auth=False)
        return d.get("offers", []) if isinstance(d, dict) else d

    def instances(self) -> list[dict]:
        out, params = [], {"select_filters": {}, "order_by": [{"col": "id", "dir": "asc"}], "limit": 25}
        while True:
            d = self._call("GET", "/api/v1/instances/", params)
            out += d.get("instances") or []
            if not d.get("next_token"):
                return out
            params["after_token"] = d["next_token"]

    def create(self, offer_id: int, body: dict) -> dict:
        return self._call("PUT", f"/asks/{int(offer_id)}/", body=body)

    def destroy(self, instance_id: int) -> dict:
        return self._call("DELETE", f"/instances/{int(instance_id)}/", body={})


class Scaler:
    def __init__(self, cfg, vast: Vast | None = None, clock=time.time, engine=None):
        self.cfg = cfg
        g = lambda k, d: cfg.get("scaler", k, d)
        self.dry_run = bool(g("dry_run", True))
        self.tick_seconds = float(g("tick_seconds", 60))
        self.target_s = float(g("target_minutes", 30)) * 60
        self.start_backlog = int(g("start_backlog", 500))     # rent only while more games than this wait
        self.idle_s = float(g("idle_minutes", 10)) * 60
        self.min_s = float(g("min_minutes", 20)) * 60
        self.boot_s = float(g("boot_minutes", 15)) * 60
        self.max_per_hour = float(g("max_per_hour", 1.0))
        self.max_per_day = float(g("max_per_day", 10.0))
        self.max_instances = int(g("max_instances", 6))
        # big machines: each new one pulls the image and compiles every bot it meets, once
        self.min_threads = int(g("min_threads", 64))
        self.min_reliability = float(g("min_reliability", 0.98))
        self.slots_per_thread = float(g("slots_per_thread", 1.0))      # one game per hardware thread
        self.ram_per_slot = float(g("ram_per_slot_gb", 1.5))
        self.disk_gb = float(g("disk_gb", 40))
        self.max_bandwidth = float(g("max_bandwidth_per_gb", 0.02))
        self.max_per_slot_hour = float(g("max_per_slot_hour", 0.02))
        # interruptible (bid) machines: much cheaper; outbid ones pause, and one paused for
        # paused_minutes is returned and replaced
        self.interruptible = bool(g("interruptible", True))
        self.bid_margin = float(g("bid_margin", 1.15))
        self.paused_s = float(g("paused_minutes", 10)) * 60
        # safety: rentals per hour, boot failures in a row before renting stops (until --resume),
        # and how long the engine may be unreachable before every machine is returned
        self.max_rents_per_hour = int(g("max_rents_per_hour", 4))
        self.max_boot_failures = int(g("max_boot_failures", 2))
        self.engine_down_s = float(g("engine_down_minutes", 10)) * 60
        # the worker's own dead-man switch on the rented machine (arena/worker.py self_destroy)
        self.worker_idle_s = float(g("worker_idle_minutes", 30)) * 60
        self.worker_orphan_minutes = float(g("worker_orphan_minutes", 20))
        self.image = g("image", "registry.syeyoung.dev/undertehc/tokenz-arena-worker:latest")
        self.arena_url = g("arena_url", "https://tokenz-arena.lonk-kelvin.ts.net")
        self.tz = timezone(timedelta(hours=float(cfg.get("api", "utc_offset_hours", 9))))
        secrets_dir = Path(g("secrets_dir", "/secrets"))
        self.secrets_dir = secrets_dir
        key = _read(Path(g("key_file", secrets_dir / "vast-key")))
        self.vast = vast or Vast(key)
        self.engine = engine or f"http://127.0.0.1:{cfg.get('api', 'engine_port', 8701)}"
        self.clock = clock
        self.path = cfg.data_dir / "scaler" / "state.json"
        self.state = store.read_json(self.path, None) or {"instances": {}, "spend": {}, "log": []}
        self.state.setdefault("instances", {})
        self.state.setdefault("spend", {})
        self.state.setdefault("log", [])
        self.state.setdefault("paused", False)
        self.state.setdefault("rents", [])
        self.state.setdefault("boot_failures", 0)

    # ------------------------------------------------------------------ bookkeeping
    def today(self) -> str:
        return datetime.fromtimestamp(self.clock(), self.tz).strftime("%Y-%m-%d")

    def spent_today(self) -> float:
        return float(self.state["spend"].get(self.today(), 0.0))

    def per_hour(self) -> float:
        return sum(i.get("dph", 0) for i in self.state["instances"].values())

    def note(self, msg: str, **extra):
        log.info("scaler: %s", msg)
        self.state["log"] = (self.state["log"] + [{"at": iso(self.clock()), "msg": msg, **extra}])[-300:]

    def save(self, **extra):
        self.state.update(updated=iso(self.clock()), dry_run=self.dry_run,
                          caps={"per_hour": self.max_per_hour, "per_day": self.max_per_day,
                                "instances": self.max_instances},
                          per_hour=round(self.per_hour(), 4), spent_today=round(self.spent_today(), 4),
                          **extra)
        store.write_json(self.path, self.state)

    def account(self):
        """Adds what the rented machines cost since the last tick to today's spend. The last
        tick's time is saved, so time the scaler was down (machines still billing) counts too."""
        t = self.clock()
        last = self.state.get("tick_t")
        if last is not None and t > last:
            hours = (t - last) / 3600
            day = self.today()
            self.state["spend"][day] = self.spent_today() + self.per_hour() * hours
            for k in sorted(self.state["spend"])[:-14]:
                self.state["spend"].pop(k, None)
        self.state["tick_t"] = t

    # ------------------------------------------------------------------ the engine
    def queue(self) -> dict:
        with urllib.request.urlopen(f"{self.engine}/api/v1/queue", timeout=20) as r:
            return json.loads(r.read())

    # ------------------------------------------------------------------ offers
    def query(self) -> dict:
        return {"verified": {"eq": True}, "rentable": {"eq": True},
                "cpu_cores_effective": {"gte": self.min_threads},
                "reliability2": {"gte": self.min_reliability},
                "disk_space": {"gte": self.disk_gb},
                "inet_up_cost": {"lte": self.max_bandwidth}, "inet_down_cost": {"lte": self.max_bandwidth},
                "type": "bid" if self.interruptible else "on-demand",
                "order": [["dph_total", "asc"]], "limit": 300}

    def price_of(self, offer: dict) -> float:
        """What we'd pay per hour: on-demand, the offer's price; interruptible, our bid (the
        minimum bid plus a margin, so a small rise doesn't pause us at once)."""
        if not self.interruptible:
            return float(offer["dph_total"])
        return round(float(offer["dph_total"]) * self.bid_margin, 4)

    def slots_of(self, offer: dict) -> int:
        threads = float(offer.get("cpu_cores_effective") or 0)
        ram_gb = float(offer.get("cpu_ram") or 0) / 1024
        return max(0, int(min(threads * self.slots_per_thread, ram_gb / self.ram_per_slot)))

    def ranked_offers(self) -> list[dict]:
        out = []
        for o in self.vast.offers(self.query()):
            if float(o.get("cpu_cores_effective") or 0) < self.min_threads:
                continue
            slots = self.slots_of(o)
            if slots < 4 or not o.get("dph_total"):
                continue
            price = self.price_of(o)
            per_slot = price / slots
            if per_slot > self.max_per_slot_hour:
                continue
            out.append({**o, "_slots": slots, "_per_slot": per_slot, "_price": price})
        out.sort(key=lambda o: o["_per_slot"])
        return out

    # ------------------------------------------------------------------ instances
    def launch(self, offer: dict) -> dict | None:
        name = f"vast-{secrets.token_hex(3)}"
        token = secrets.token_urlsafe(32)
        slots = offer["_slots"]
        body = {"client_id": "me", "image": self.image, "disk": self.disk_gb, "label": f"{LABEL}:{name}",
                "runtype": "args", "args": ["worker"],
                "env": {"ARENA_WORKER_TOKEN": token, "TS_AUTHKEY": _read(self.secrets_dir / "worker-ts-authkey") or "",
                        "ARENA_URL": self.arena_url, "WORKER_NAME": name, "ARENA_SLOTS": str(slots),
                        # the machine destroys itself if the worker stops: idle, no arena, revoked
                        "ARENA_SELF_DESTROY": "1", "ARENA_IDLE_EXIT": str(int(self.worker_idle_s)),
                        "ARENA_ORPHAN_MINUTES": str(self.worker_orphan_minutes)}}
        if self.interruptible:
            body["price"] = offer["_price"]          # our bid, $/hour
        login = _read(self.secrets_dir / "registry-login")
        if login:                                   # "user password" (one line)
            user, _, password = login.partition(" ")
            body["image_login"] = f"-u {user} -p {password} {self.image.split('/')[0]}"
        with tokens_file(self.cfg) as d:
            d[name] = {"token": token, "created": iso(self.clock()), "offer": offer.get("id")}
        try:
            r = self.vast.create(offer["id"], body)
        except VastError as e:
            with tokens_file(self.cfg) as d:
                d.pop(name, None)
            self.note(f"couldn't rent offer {offer.get('id')}: {e}")
            return None
        self.state["rents"] = [x for x in self.state["rents"] if self.clock() - x < 3600] + [self.clock()]
        iid = r.get("new_contract") or r.get("id")
        inst = {"id": iid, "name": name, "offer": offer.get("id"), "dph": offer["_price"],
                "interruptible": self.interruptible,
                "slots": slots, "threads": offer.get("cpu_cores_effective"), "cpu": offer.get("cpu_name"),
                "where": offer.get("geolocation"), "created": iso(self.clock()), "created_t": self.clock(),
                "busy_t": self.clock()}
        self.state["instances"][name] = inst
        self.note(f"rented {name}: {inst['cpu']} ({slots} slots, ${inst['dph']:.3f}/h"
                  f"{' interruptible' if self.interruptible else ''}, {inst['where']})")
        return inst

    def retire(self, name: str, why: str):
        inst = self.state["instances"].get(name)
        if not inst:
            return
        try:
            if inst.get("id") is not None:
                self.vast.destroy(inst["id"])
        except VastError as e:
            self.note(f"couldn't destroy {name}: {e}")
            return
        with tokens_file(self.cfg) as d:
            if name in d:
                d[name]["revoked"] = True
        self.state["instances"].pop(name, None)
        self.note(f"returned {name} ({why})")

    def reconcile(self):
        """Matches our records with Vast's: adopts nothing unknown (destroys it), forgets what's gone."""
        live = {}
        for i in self.vast.instances():
            label = str(i.get("label") or "")
            if label.startswith(LABEL + ":"):
                live[label.split(":", 1)[1]] = i
        for name, i in live.items():
            if name not in self.state["instances"]:
                try:
                    self.vast.destroy(i["id"])
                    self.note(f"destroyed {name}: a labelled instance this scaler doesn't know")
                except VastError as e:
                    self.note(f"couldn't destroy unknown {name}: {e}")
        for name in list(self.state["instances"]):
            if name not in live:
                with tokens_file(self.cfg) as d:
                    if name in d:
                        d[name]["revoked"] = True
                self.state["instances"].pop(name, None)
                self.note(f"{name} is gone from Vast (ended outside the scaler)")
            else:
                inst = self.state["instances"][name]
                status = live[name].get("actual_status")
                inst["status"] = status
                if inst.get("seen_t") and status not in ("running", "loading", None):
                    inst.setdefault("paused_t", self.clock())      # outbid (interruptible) or stopped
                else:
                    inst.pop("paused_t", None)

    # ------------------------------------------------------------------ the decision
    def plan(self, q: dict) -> dict:
        """How many rented slots the queue needs now (pure: no side effects)."""
        left = int((q.get("totals") or {}).get("left") or 0)
        running = len(q.get("running") or [])
        avg = float(q.get("avg_game_s") or 30)
        ours = set(self.state["instances"])
        remote = q.get("remote") or []
        own = int((q.get("slots") or {}).get("local") or 0) + sum(
            w.get("slots", 0) for w in remote if w.get("name") not in ours)
        needed = math.ceil((left + running) * avg / self.target_s) if left + running else 0
        want = max(0, needed - own)
        have = sum(i["slots"] for i in self.state["instances"].values() if not i.get("paused_t"))
        return {"left": left, "running": running, "avg_game_s": avg, "own_slots": own,
                "needed_slots": needed, "want_rented": want, "rented_slots": have}

    def tick(self):
        if not self.dry_run:
            self.reconcile()
        self.account()
        t = self.clock()
        try:
            q = self.queue()
            self.state["queue_ok_t"] = t
        except Exception as e:
            down = t - self.state.get("queue_ok_t", t)
            if down > self.engine_down_s and self.state["instances"] and not self.dry_run:
                for name in list(self.state["instances"]):
                    self.retire(name, f"the engine has been unreachable for {down / 60:.0f} min")
            self.save(error=f"engine unreachable: {e}"[:300])
            return {"decision": "engine unreachable"}
        p = self.plan(q)
        busy = {w["name"]: w for w in (q.get("remote") or [])}
        for name, inst in self.state["instances"].items():
            w = busy.get(name)
            if w is not None:
                if not inst.get("seen_t"):
                    self.state["boot_failures"] = 0
                inst["seen_t"] = t
                if w.get("running"):
                    inst["busy_t"] = t
        decision = "hold"
        if self.state.get("paused"):
            decision = "paused"
        elif self.spent_today() >= self.max_per_day:
            for name in list(self.state["instances"]):
                self.retire(name, f"daily cap ${self.max_per_day:.2f} reached")
            decision = "daily cap reached"
        else:
            # boot failures: never showed up as a worker; paused too long: outbid
            for name, inst in list(self.state["instances"].items()):
                if not inst.get("seen_t") and t - inst["created_t"] > self.boot_s:
                    self.retire(name, f"never joined within {self.boot_s / 60:.0f} min")
                    self.state["boot_failures"] += 1
                    if self.state["boot_failures"] >= self.max_boot_failures:
                        self.state["halted"] = (f"{self.state['boot_failures']} machines in a row never joined; "
                                                "renting stopped until `arena scaler --resume`")
                        self.note(self.state["halted"])
                elif inst.get("paused_t") and t - inst["paused_t"] > self.paused_s:
                    self.retire(name, f"paused (outbid) for {self.paused_s / 60:.0f} min")
            if p["want_rented"] > p["rented_slots"] and p["left"] <= self.start_backlog:
                decision = f"backlog {p['left']} games (renting starts above {self.start_backlog})"
            elif p["want_rented"] > p["rented_slots"]:
                decision = self.grow(p)
            elif p["want_rented"] < p["rented_slots"]:
                decision = self.shrink(p, t)
        p["decision"] = decision
        self.save(plan=p, error=None)
        return p

    def grow(self, p: dict) -> str:
        if self.state.get("halted"):
            return "halted: " + self.state["halted"]
        if len(self.state["instances"]) >= self.max_instances:
            return f"at max instances ({self.max_instances})"
        recent = [x for x in self.state["rents"] if self.clock() - x < 3600]
        if len(recent) >= self.max_rents_per_hour:
            return f"rented {len(recent)} machines in the last hour (the limit); waiting"
        budget = self.max_per_hour - self.per_hour()
        gap = p["want_rented"] - p["rented_slots"]
        # a new machine runs at least min_minutes: that must fit under today's cap too
        left_today = self.max_per_day - self.spent_today()
        offers = [o for o in self.ranked_offers() if o["_price"] <= budget
                  and (self.per_hour() + o["_price"]) * self.min_s / 3600 <= left_today]
        if not offers:
            return f"need {gap} more slots, but no offer fits the ${budget:.2f}/h left under the cap"
        # cheapest per slot, bigger first on ties: fewer, bigger machines (each new one pays to
        # pull the image and compile the bots); one per tick, so a big gap fills over minutes
        o = min(offers, key=lambda o: (round(o["_price"] / o["_slots"], 5), -o["_slots"]))
        what = (f"need {gap} more slots: rent {o.get('cpu_name')} ({o['_slots']} slots, "
                f"${o['_price']:.3f}/h{' interruptible' if self.interruptible else ''}, {o.get('geolocation')})")
        if self.dry_run:
            self.note("[dry run] would " + what.replace(": rent", ": rent"))
            return "[dry run] " + what
        inst = self.launch(o)
        return what if inst else "rent failed"

    def shrink(self, p: dict, t: float) -> str:
        spare = p["rented_slots"] - p["want_rented"]
        idle = sorted((i for i in self.state["instances"].values()
                       if t - i["created_t"] >= self.min_s and t - i.get("busy_t", i["created_t"]) >= self.idle_s
                       and i["slots"] <= spare), key=lambda i: -i["dph"])
        if not idle:
            return f"{spare} slots more than needed; none idle for {self.idle_s / 60:.0f} min yet"
        i = idle[0]
        if self.dry_run:
            return f"[dry run] would return {i['name']}"
        self.retire(i["name"], f"backlog needs {p['want_rented']} rented slots, idle {self.idle_s / 60:.0f} min")
        return f"returned {i['name']}"

    def run_forever(self):
        self.note(f"started ({'dry run' if self.dry_run else 'live'}; caps ${self.max_per_hour}/h, "
                  f"${self.max_per_day}/day, {self.max_instances} instances)")
        while True:
            try:
                self.tick()
            except Exception as e:                     # keep running; say what went wrong
                log.exception("scaler tick failed")
                self.save(error=f"{type(e).__name__}: {e}"[:300])
            time.sleep(self.tick_seconds)


def _read(p: Path) -> str | None:
    try:
        return Path(p).read_text().strip() or None
    except OSError:
        return None


def main(args) -> int:
    from arena import config
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    lock_path = cfg.data_dir / "scaler" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)       # one scaler at a time
    except OSError:
        raise SystemExit("another scaler is running (data/scaler/.lock)")
    s = Scaler(cfg)
    if getattr(args, "resume", False):
        s.state.pop("halted", None)
        s.state["boot_failures"] = 0
        s.note("resumed by hand")
        s.save()
        print("resumed")
        return 0
    if getattr(args, "live", False):
        s.dry_run = False
    if getattr(args, "once", False):
        print(json.dumps(s.tick(), indent=1))
        return 0
    s.run_forever()
    return 0
