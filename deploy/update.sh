#!/bin/sh
# Pull-based deploy on poscatgpu2 (run by arena-update.timer every 3 minutes, or by hand):
# fast-forward tokenz-arena and bcweb to origin/main, and if either moved, rebuild the image,
# smoke-test it, and restart the arena. A failed build or smoke test leaves the old one running.
#   deploy/update.sh [--force]
set -eu
cd "$(dirname "$0")/.."
. ./.env
log() { echo "$(date -u +%FT%TZ) $*"; }

git fetch -q origin main
git -C "${BCWEB_DIR:-../bcweb}" fetch -q origin main
if [ "${1:-}" != "--force" ] \
   && [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
   && [ "$(git -C "${BCWEB_DIR:-../bcweb}" rev-parse HEAD)" = "$(git -C "${BCWEB_DIR:-../bcweb}" rev-parse origin/main)" ]; then
  exit 0
fi
git merge -q --ff-only origin/main
git -C "${BCWEB_DIR:-../bcweb}" merge -q --ff-only origin/main
log "deploying $(git log -1 --format='%h %s')"

mkdir -p "$ARENA_DATA" "$ARENA_CACHE" "$ARENA_SECRETS"
[ -f "$ARENA_DATA/arena.toml" ] || cp deploy/arena.poscatgpu2.toml "$ARENA_DATA/arena.toml"
docker compose build -q arena
# Smoke test on the bare image (no network: the compose service shares the sidecar's).
docker run --rm --network none tokenz-arena:latest \
  python -c "import arena.api.http, arena.daemon, arena.cli; print('smoke ok')"
docker compose up -d
log "deployed $(git rev-parse --short HEAD)"
