# Scaler: rented workers on vast.ai

When the queue is long, the arena rents machines on [vast.ai](https://vast.ai) and runs the
normal worker on them (the official judge, `unswbc run --sandbox`), and returns them when the
backlog is gone. Compose service `scaler` (`arena scaler`), its own process. Code:
`arena/scaler.py`.

## What it decides, every minute

    needed slots = ceil((games left + running) × average game length / target_minutes)
    rent         = needed slots − our own slots (the engine's judges, poscatgpu1, laptops…)

- **Grow:** rent one machine per minute while the backlog needs more slots. It picks the cheapest
  offer per slot it would actually use: a 128-slot box for a 30-slot gap pays for 98 idle slots.
  Offers must be verified hosts with reliability ≥ 0.98, ≥ 32 threads, ≥ 1.5 GB RAM per slot,
  ≥ 40 GB disk, and bandwidth ≤ $0.02/GB. Slots = threads ÷ 2 (one game per physical core).
- **Shrink:** return a machine when the backlog no longer needs it, it has had no game for
  `idle_minutes` (10), and it's been up at least `min_minutes` (20).
- **Never past the caps:** `max_per_hour` ($1), `max_per_day` ($10; at the cap everything is
  returned), `max_instances` (6).
- **Broken machines:** one that never joins within `boot_minutes` (15) is returned.
- **Housekeeping:** instances are labelled `tokenz-worker:<name>`. A labelled instance the scaler
  doesn't know (left over from a crash) is destroyed; one destroyed outside the scaler is forgotten.

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
| `target_minutes` | 30 | Clear the backlog within this long. |
| `max_per_hour`, `max_per_day`, `max_instances` | 1.0, 10.0, 6 | Caps. |
| `idle_minutes`, `min_minutes`, `boot_minutes` | 10, 20, 15 | Shrinking and broken-machine timeouts. |
| `min_threads`, `min_reliability`, `ram_per_slot_gb`, `disk_gb`, `max_bandwidth_per_gb`, `max_per_slot_hour` | 32, 0.98, 1.5, 40, 0.02, 0.02 | Which offers qualify. |
| `slots_per_thread` | 0.5 | Games per hardware thread. |
| `image` | `registry.syeyoung.dev/tokenz-arena:latest` | The worker image. |
| `secrets_dir` | `/secrets` | Holds `vast-key`, `worker-ts-authkey` and `registry-login` (`user password`). |

`arena scaler --once` makes one decision and prints it.
