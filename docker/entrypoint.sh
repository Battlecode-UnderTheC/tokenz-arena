#!/bin/sh
# Entry point of the tokenz-arena image.
#   <image> worker [arena worker options]   a remote judge, configured by environment:
#       ARENA_WORKER_TOKEN   (required) a token from the arena's [workers] tokens
#       ARENA_URL            the arena (default https://tokenz-arena.lonk-kelvin.ts.net)
#       TS_AUTHKEY           join the tailnet with this key first (ephemeral, tag:arena-worker);
#                            leave it out on a machine that already reaches the arena
#       WORKER_NAME, ARENA_SLOTS, ARENA_IDLE_EXIT (seconds idle before exiting; for batch jobs)
#   <image> anything else                   runs it (the engine and website use `arena run|web`)
set -eu

if [ "${1:-}" != "worker" ]; then
  exec "$@"
fi
shift

name="${WORKER_NAME:-${AWS_BATCH_JOB_ID:-$(hostname)}}"
name=$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-60)
export WORKER_NAME="$name"

if [ -n "${TS_AUTHKEY:-}" ]; then
  # Userspace Tailscale: no root or tun device, so it runs in any container (Batch, k8s, ...).
  # Tailnet traffic goes through its local HTTP proxy; state lives in memory, so with an
  # ephemeral key the node disappears from the tailnet when the container stops.
  sock=/tmp/tailscaled.sock
  tailscaled --tun=userspace-networking --state=mem: --socket="$sock" \
    --outbound-http-proxy-listen=127.0.0.1:1055 >/tmp/tailscaled.log 2>&1 &
  ts_pid=$!
  i=0
  until tailscale --socket="$sock" status >/dev/null 2>&1 || [ $i -ge 30 ]; do
    i=$((i + 1)); sleep 1
    tailscale --socket="$sock" up --auth-key="$TS_AUTHKEY" --hostname="arena-worker-$name" \
      --accept-dns=false >/dev/null 2>&1 || true
  done
  if ! tailscale --socket="$sock" status >/dev/null 2>&1; then
    echo "worker: couldn't join the tailnet; tailscaled said:" >&2
    tail -20 /tmp/tailscaled.log >&2
    exit 1
  fi
  echo "worker: on the tailnet as arena-worker-$name"
  export HTTPS_PROXY=http://127.0.0.1:1055 HTTP_PROXY=http://127.0.0.1:1055 NO_PROXY=localhost,127.0.0.1
  trap 'tailscale --socket="$sock" logout >/dev/null 2>&1; kill $ts_pid 2>/dev/null' EXIT
  arena worker --name "$name" "$@" &
  child=$!
  trap 'kill -TERM $child 2>/dev/null' TERM INT
  wait $child || true
  wait $child 2>/dev/null || true
  exit 0
fi

exec arena worker --name "$name" "$@"
