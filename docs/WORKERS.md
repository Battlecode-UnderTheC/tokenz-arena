# Remote workers: machines that join and leave

Status: built on branch `remote-workers`, not on `main` yet. Tested on a Mac: an engine with no
local judges and one `arena worker` played every game through the protocol (35 games in its first
minute, replays included). Replaying remote games locally gave identical results (winner, rounds,
end reason).

## Goal

Any machine with Docker and a Tailscale key can add judge capacity to the arena, and leave
again, without the arena being told in advance. Examples: another lab server, a laptop overnight,
a rented box on any cloud, a Kubernetes Job. Nothing ties the arena to one provider: a worker is
"run this container".

Non-goals:
- No external queue or object store. The engine already decides what to play, and at about one
  game a second its own HTTP API is enough.
- Workers never decide what to play, and never write the arena's state. They lease games and
  return results.

## Pieces

```
            tailnet (https://tokenz-arena.<tailnet>.ts.net)
                              |
  worker A ─┐          web (arena web) ── forwards ──> engine (arena run)
  worker B ─┼── lease / heartbeat / result ───────────>   scheduler, leases, records results
  worker C ─┘   bot zips, maps (served by web from disk)   built-in local pool = one more worker
```

- **Engine** (`arena run`). Owns the queue (`arena/queue.py`). New: a worker
  registry and leases. The built-in process pool stays as the engine's own worker, so a single
  machine works exactly as now.
- **Website** (`arena web`). Serves the workers' downloads (bot zips, map text) straight from the
  data folder, and forwards lease, heartbeat and result calls to the engine, like every write.
- **Worker** (`arena worker`, same image). A loop: lease games, fetch and cache bots and maps,
  run the judge in its own process pool, return results with replays, heartbeat.

## Protocol (under `/api/v1/workers`, worker token required)

| Call | Body / reply |
|---|---|
| `POST /lease` | `{worker, version, slots}` → `{leases: [{lease, game}], lease_seconds, heartbeat_seconds}`. Up to `slots` games, or `[]` when there's nothing to do. A `game` has `a`/`b` (submission id and source sha256), `suite`, `map` (name and sha256), `seed`, `timeout`. |
| `POST /heartbeat` | `{worker, version, slots, leases: [...]}` → `{renewed: [...], cancelled: [...]}`. Renews the worker's leases and keeps it in the registry. |
| `POST /result` | multipart: `meta` (JSON: `lease`, `winner`, `rounds`, `reason`, `seconds`, `stats`, `error`) and `replay` (file, optional) → `{recorded: true}` or `{recorded: false, why}` |
| `GET /bot/{id}` | the submission's zip. The worker checks it against the sha256 from the lease, and caches it by sha. |
| `GET /map/{suite}/{name}` | the map file. The worker checks its sha256 and caches it. |
| `GET /workers` (public read) | the registry: name, host, version, slots, running, games in the last hour, last seen. |

## Rules

- **Leases.** A leased game counts as running, so it's never handed out twice. A lease lasts
  `lease_seconds` (default 120). Heartbeats every 30 s renew it. If it expires (the worker died,
  or lost its network), the engine drops it and the game is owed again; the next lease hands it
  out. A result for an expired lease is dropped, so a game is never counted twice.
- **Same judge, same results.** A worker sends its unswbc version with every call. A version that
  isn't the engine's is refused (409), because results from another judge version mustn't mix
  with ratings (the same rule as engine pinning). Games are deterministic given the seed and the
  sandbox's CPU points, so where a game runs doesn't change its result.
- **Provenance.** Each game records `meta.worker` (name and host), so a misbehaving machine's
  games can be found and replayed.
- **Trust.** Results move ratings, so leasing and returning need a worker token (`[workers]
  token` in arena.toml; a worker sends `Authorization: Bearer <token>`). Being on the tailnet
  isn't enough on its own. Tokens can be listed and revoked by name.
- **Holdout maps stay home.** Holdout games are never leased to remote workers: their maps must
  not leave poscatgpu2. The engine's own pool plays them.
- **Fairness and priorities don't change.** The engine picks what to lease with the same
  `next_jobs` as for its own pool: drafts before placement before top-ups, owners in turn.
  Workers can't choose.
- **Capacity.** Total slots = the local pool + the sum of live workers' free slots. Nothing to
  configure: a worker's slots appear when it leases and disappear when its heartbeats stop.

## Worker

One image, one command. `<image>` is `ghcr.io/battlecode-underthec/tokenz-arena:latest`, built by
CI from `main` (`.github/workflows/image.yml`). The package is private, so pull it with a GitHub
token that has `read:packages` (`docker login ghcr.io`).

| Variable | |
|---|---|
| `ARENA_WORKER_TOKEN` | Required: a token from the arena's `[workers] tokens`. |
| `TS_AUTHKEY` | The workers' Tailscale auth key (reusable, ephemeral, pre-approved, tagged `tag:tokens-worker`), minted on poscatgpu2 by `arena tailscale-key` into `~/.config/tokenz-arena/worker-ts-authkey`. The worker joins the tailnet itself in userspace mode: no root, no `/dev/net/tun`, no `--privileged`. Leave it out on a machine that already reaches the arena. |
| `ARENA_URL` | Default `https://tokenz-arena.lonk-kelvin.ts.net`. |
| `WORKER_NAME` | Default: the Batch job id, else the host name. |
| `ARENA_SLOTS` | Games at once. Default: cores ÷ 1.5. |
| `ARENA_IDLE_EXIT` | Exit after this many idle seconds. Use it for batch jobs, so they end when the queue is empty. |

**Any machine with Docker:**

```sh
docker run -d --restart unless-stopped --name arena-worker \
  -e ARENA_WORKER_TOKEN=... -e TS_AUTHKEY=tskey-auth-... \
  -v arena-worker-cache:/home/arena/.cache \
  ghcr.io/battlecode-underthec/tokenz-arena:latest worker
```

The volume keeps the judge's prepared toolchain and compiled bots across restarts.

**AWS Batch** (any instance type; Fargate works too). A job definition:

```json
{
  "jobDefinitionName": "tokenz-arena-worker",
  "type": "container",
  "containerProperties": {
    "image": "ghcr.io/battlecode-underthec/tokenz-arena:latest",
    "command": ["worker"],
    "resourceRequirements": [{"type": "VCPU", "value": "16"}, {"type": "MEMORY", "value": "16384"}],
    "environment": [{"name": "ARENA_IDLE_EXIT", "value": "600"}, {"name": "ARENA_SLOTS", "value": "12"}],
    "secrets": [{"name": "ARENA_WORKER_TOKEN", "valueFrom": "arn:aws:secretsmanager:...:arena-worker-token"},
                {"name": "TS_AUTHKEY", "valueFrom": "arn:aws:secretsmanager:...:tailscale-worker-key"}],
    "repositoryCredentials": {"credentialsParameter": "arn:aws:secretsmanager:...:ghcr-pull"}
  }
}
```

Submit N copies (an array job) when the queue is long. Each copy drains games and exits after
`ARENA_IDLE_EXIT` seconds with nothing to do. A fresh container prepares the judge's toolchain on
its first game (a minute or two), so jobs are best long-lived.

**Kubernetes:** a Deployment, or a Job with `parallelism: N`. The container is the image with args
`["worker"]` and the same variables, with the token and key from a Secret. No special security
context is needed.

**Without Docker,** on a machine on the tailnet with the repo checked out:

```sh
uv run arena worker --arena https://tokenz-arena.<tailnet>.ts.net --token ... --slots 16
```

- **Cache.** Bots by source sha256 (zip, unpacked, and the judge's compiled wasm, which is keyed
  by folder name and content, so a C++ bot compiles once per machine). Maps by sha256.
- **Slots.** `--slots` defaults to the core count divided by 1.5, since a game uses one to three
  cores at peak.
- **Leaving.** `SIGTERM` stops leasing, finishes the running games (up to 10 minutes; an abandoned
  lease just expires and the game is played again), and exits.

## The Tailscale key

`arena tailscale-key` (run on poscatgpu2) uses a Tailscale OAuth client with the Auth Keys scope
(`~/.config/tokenz-arena/tailscale-oauth-id` and `tailscale-oauth`) to mint the workers' key:
reusable, ephemeral, pre-approved, tagged `tag:tokens-worker`, 90 days. Workers only ever get that
key, never the OAuth client. Re-run it before the key expires. The tailnet policy limits
`tag:tokens-worker` to `tokenz-arena:443`, so a worker can reach the arena and nothing else.

## Turning it on

- Engine: add `[workers] tokens = { name = "a long random token", ... }` to arena.toml. Without
  tokens, nothing changes: no leases, local judges only.
- Checks have their own slots now (`[play] check_slots`, default 2), so new submissions are
  checked even when every game goes to remote workers.
- `[play] workers` stays the engine's own judge count; it can go to 0 if remote capacity is enough.

## Engine changes

- `arena/workers.py`: the registry, leases, and the reaper (every 10 s) that drops expired leases
  and workers silent for more than 3 heartbeats.
- `Daemon`: the local pool becomes one more worker. `_play` splits into running a game (local
  pool) and `record_result(job, result, worker)`, which remote results share.
- `arena.running` entries gain `worker` and `expires`. The heartbeat file includes the registry,
  so the website's Queue page can show workers.
- Routes in `arena/api/workers.py`, mounted in the engine. The website serves `GET` downloads from
  disk and forwards the rest.

## Website

- **Queue page:** a Workers table (name, host, version, slots, running, games in the last hour,
  last seen).
- **Game page:** "played on <worker>".

## Rollout

1. The local pool keeps working unchanged. Remote workers are opt-in: no token configured, no
   leases.
2. Try one remote worker on the Mac against the live arena. Compare its results with local results
   for the same games (same seed, same bots, same map), which should be identical.
3. Add machines.
