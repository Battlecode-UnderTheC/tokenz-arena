#!/bin/sh
# Pull-based deploy on poscatgpu2 (run by arena-update.timer every 3 minutes, or by hand):
# fast-forward tokenz-arena and bcweb to origin/main, and if that's not what last deployed,
# rebuild the image, smoke-test it, and restart the engine and the website. A failed build or
# smoke test leaves the old ones running, and the next run tries again.
#   deploy/update.sh [--force]
set -eu
cd "$(dirname "$0")/.."
. ./.env
log() { echo "$(date -u +%FT%TZ) $*"; }

git fetch -q origin main
git -C "${BCWEB_DIR:-../bcweb}" fetch -q origin main
git merge -q --ff-only origin/main
git -C "${BCWEB_DIR:-../bcweb}" merge -q --ff-only origin/main
# Deploy when what's checked out differs from what last deployed successfully, so a failed
# deploy is retried on the next run instead of being skipped as "already up to date".
want="$(git rev-parse HEAD) $(git -C "${BCWEB_DIR:-../bcweb}" rev-parse HEAD)"
stamp=.deployed
if [ "${1:-}" != "--force" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$want" ]; then
  exit 0
fi
log "deploying $(git log -1 --format='%h %s')"

mkdir -p "$ARENA_DATA" "$ARENA_CACHE" "$ARENA_SECRETS"
[ -f "$ARENA_DATA/arena.toml" ] || cp deploy/arena.poscatgpu2.toml "$ARENA_DATA/arena.toml"
old=$(docker image inspect tokenz-arena:latest -f '{{.Id}}' 2>/dev/null || true)
docker compose build -q engine          # web runs the same image
# Smoke test on the bare image (no network: the compose service shares the sidecar's).
docker run --rm --network none tokenz-arena:latest \
  python -c "import arena.api.http, arena.daemon, arena.cli; print('smoke ok')"
docker rm -f tokenz-arena-arena-1 >/dev/null 2>&1 || true   # the old single-process service
docker compose up -d tailscale   # first, so the others have a namespace to join
docker compose up -d --remove-orphans
echo "$want" > "$stamp"
# The minimal worker image rented machines pull (docker/worker.Dockerfile, docs/SCALER.md),
# with the engine's unswbc and wasmtime: built and pushed when the registry login is set up
# ($ARENA_SECRETS/registry-push: "user password"). A failure doesn't fail the deploy.
REG_IMAGE="${REGISTRY_IMAGE:-registry.syeyoung.dev/undertehc/tokenz-arena-worker}"
if [ -f "$ARENA_SECRETS/registry-push" ]; then
  read -r reg_user reg_pass < "$ARENA_SECRETS/registry-push"
  unswbc_v=$(sed -n 's/.*"unswbc==\([0-9.]*\)".*/\1/p' pyproject.toml | head -1)
  wasmtime_v=$(awk '/^name = "wasmtime"/{getline; gsub(/[^0-9.]/, ""); print; exit}' uv.lock)
  if docker build -q -f docker/worker.Dockerfile --build-arg UNSWBC="$unswbc_v" --build-arg WASMTIME="$wasmtime_v" \
       -t "$REG_IMAGE:latest" . >/dev/null \
     && docker run --rm --network none --entrypoint arena "$REG_IMAGE:latest" worker --help >/dev/null \
     && printf '%s' "$reg_pass" | docker login "${REG_IMAGE%%/*}" -u "$reg_user" --password-stdin >/dev/null 2>&1 \
     && docker push -q "$REG_IMAGE:latest" >/dev/null; then
    log "pushed $REG_IMAGE:latest"
    # the engine drains rented machines on any other worker code, one at a time, and the
    # scaler replaces them (arena/workers.py Registry.draining)
    build=$(docker run --rm --network none --entrypoint python "$REG_IMAGE:latest" \
              -c "from arena.worker import build_id; print(build_id())" 2>/dev/null || true)
    [ -n "$build" ] && printf '%s\n' "$build" > "$ARENA_DATA/worker-image.build"
  else
    log "push to $REG_IMAGE failed (deploy is fine; rented workers keep the previous image)"
  fi
  unset reg_user reg_pass
fi
# Remove the image this deploy replaced (only ours: the previous tokenz-arena:latest), so
# deploys don't pile up images on a Docker shared with other people.
new=$(docker image inspect tokenz-arena:latest -f '{{.Id}}')
if [ -n "$old" ] && [ "$old" != "$new" ]; then docker rmi "$old" >/dev/null 2>&1 || true; fi
log "deployed $(git rev-parse --short HEAD)"
