# Scaler: rented workers on vast.ai

When the queue is long, the arena rents machines on [vast.ai](https://vast.ai) and runs the
normal worker on them (the official judge, `unswbc run --sandbox`), and returns them when the
backlog is gone. Compose service `scaler` (`arena scaler`), its own process. Code:
`arena/scaler.py`.

## What it decides, every minute

    needed slots = ceil((games left + running) × average game length / target_minutes (5))
    rent         = needed slots − our own slots (the engine's judges, poscatgpu1, laptops…)

- **Grow:** only while more than `start_backlog` (500) games wait: rent one machine per minute
  while the backlog needs more slots: the cheapest per
  slot, bigger first. Big machines win because each new one pulls the image and compiles every
  bot it meets once. Offers must be verified hosts with reliability ≥ 0.98, ≥ 64 threads, ≥ 1.5 GB RAM per slot,
  ≥ 40 GB disk, and bandwidth ≤ $0.02/GB. Slots = threads (one game per hardware thread), fewer
  if the machine has under 1.5 GB of RAM per thread.
- **Shrink:** return a machine when the backlog no longer needs it, it has had no game for
  `idle_minutes` (10), and it's been up at least `min_minutes` (20).
- **Never past the caps:** `max_per_hour` ($1), `max_per_day` ($10; at the cap everything is
  returned), `max_instances` (6).
- **Broken machines:** one that never joins within `boot_minutes` (15) is returned.
- **Housekeeping:** instances are labelled `tokenz-worker:<name>`. A labelled instance the scaler
  doesn't know (left over from a crash) is destroyed; one destroyed outside the scaler is forgotten.

## Spending limits, and what happens when things break

| Guard | How |
|---|---|
| **$/hour** | Before renting, the sum of what's rented (on-demand price, or our bid) plus the new machine must be ≤ `max_per_hour`. Interruptible machines never cost more than our bid. |
| **$/day** | Spend is added up every minute from those prices (saved, so time the scaler was down counts too). A new machine must fit under today's cap for its minimum time; at the cap everything is returned. Days are KST. |
| **Machines** | At most `max_instances`, and at most `max_rents_per_hour` (4) rentals an hour, so a bad host can't make it churn. |
| **Machines that never join** | Returned after `boot_minutes`; after `max_boot_failures` (2) in a row, renting stops until `arena scaler --resume` (an image or registry problem shouldn't burn money). |
| **Engine down** | Unreachable for `engine_down_minutes` (10): every machine is returned. |
| **Scaler down** | Each rented worker destroys its own instance (Vast gives every container an API key for itself) when it stops: idle for 30 minutes, no arena for 20, or its token revoked. |
| **Leftovers** | Labelled instances the scaler doesn't know are destroyed; only one scaler runs at a time (a lock). |
| **The last line** | Vast bills a prepaid balance: with auto-recharge off, nothing can spend more than the credit. |

Not counted: disk (a few cents a day) and bandwidth (~$0.004/GB, ~0.5 MB per game).

Each machine gets its own worker token in `data/worker-tokens.json`, revoked when it's returned,
plus the workers' Tailscale key (which can only reach `tokenz-arena:443`). Their results count
like any worker's.

## Where to see it

- The **Queue** page: a line with rented machines, $/hour, today's spend against the cap, what
  the backlog needs, and the last decision. Rented machines appear in the judges table as
  `vast-…`.
- `GET /api/v1/scaler`: the full state, including the last 300 decisions.

## Settings (`[scaler]` in arena.toml; all optional)

| key | default | |
|---|---|---|
| `dry_run` | `true` | Decide and report only; rent nothing. `arena scaler --live` overrides. |
| `interruptible`, `bid_margin`, `paused_minutes` | true, 1.15, 0 | Rent interruptible (bid) machines, bidding the minimum +15%. One that's outbid pauses: it counts as no slots at once (a replacement is rented on the next tick), and it's destroyed after `paused_minutes` (0: on the next tick). Its games' leases expire and are played elsewhere. |
| `target_minutes` | 5 | Clear the backlog within this long. |
| `start_backlog` | 500 | Rent nothing while this many games or fewer wait. |
| `max_per_hour`, `max_per_day`, `max_instances` | 1.0, 10.0, 6 | Caps. |
| `idle_minutes`, `min_minutes`, `boot_minutes` | 10, 20, 15 | Shrinking and broken-machine timeouts. |
| `min_threads`, `min_reliability`, `ram_per_slot_gb`, `disk_gb`, `max_bandwidth_per_gb`, `max_per_slot_hour` | 64, 0.98, 1.5, 40, 0.02, 0.02 | Which offers qualify. |
| `slots_per_thread` | 1.5 | Games per hardware thread (`cpu_cores_effective`, the share vast gives a container, e.g. 64 of 256 at gpu_frac 0.25). A little over 1 covers each game's idle start-up and upload. More doesn't add throughput (machines run ~95% CPU at 1-2.6x), makes games longer (the timeout is 30 min) and runs into the container's task limit (each game's judge uses ~50 threads). A machine sized more than 20% under or 25% over today's rule is drained and replaced. RAM also caps it (`ram_per_slot_gb`). |
| `image` | `registry.syeyoung.dev/undertehc/tokenz-arena-worker:latest` | The minimal worker image (`docker/worker.Dockerfile`: Python, the judge, Tailscale and the worker's own modules; ~420 MB), built and pushed by `deploy/update.sh` on every deploy (`registry-push` holds the read-write login; rented machines get the read-only `registry-login`). |
| `max_rents_per_hour`, `max_boot_failures`, `engine_down_minutes` | 4, 2, 10 | Safety limits (above). |
| `worker_idle_minutes`, `worker_orphan_minutes` | 30, 20 | When a rented worker destroys its own instance. |
| `secrets_dir` | `/secrets` | Holds `vast-key`, `worker-ts-authkey` and `registry-login` (`user password`). |

`arena scaler --once` makes one decision and prints it.

## New worker images

Each deploy that pushes the worker image records the image's worker code (`build_id()`, a hash
of `arena/worker.py`, `judge.py`, `runner.py` and `strictjudge.py`) in
`data_dir/worker-image.build`. Rented machines (`vast-*`) report the code they run with each
lease. The engine drains a machine on other code: no new games, it finishes its own. One machine
at a time, oldest first. The scaler stops counting a draining machine's slots, so it rents a
replacement, which pulls the new image, and it returns the old machine once it's empty. Without
that file (a failed push) nothing drains. `[workers] drain_outdated = []` turns it off.
