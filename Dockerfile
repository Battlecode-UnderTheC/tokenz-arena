# tokenz-arena: the engine and website (compose.yaml), and a self-contained remote judge:
#   docker run -e ARENA_WORKER_TOKEN=... -e TS_AUTHKEY=tskey-auth-... <image> worker
# (docs/WORKERS.md). The worker brings its own Tailscale (userspace: no root, no /dev/net/tun).
# bcweb (private, optional) comes in as a build context, not over the network:
#   --build-context bcweb=../bcweb  (compose.yaml sets it)
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.* && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl && rm -rf /var/lib/apt/lists/*
# Tailscale for workers off the tailnet (only started when TS_AUTHKEY is set).
ARG TS_VERSION=1.102.4
ARG TARGETARCH=amd64
RUN curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_${TARGETARCH}.tgz" \
      | tar xz -C /tmp && mv /tmp/tailscale_*/tailscale /tmp/tailscale_*/tailscaled /usr/local/bin/ \
    && rm -rf /tmp/tailscale_*
# Runs as the host user (compose sets `user:`), so HOME must be writable by anyone.
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PATH=/app/.venv/bin:$PATH HOME=/home/arena
RUN mkdir -p /home/arena/.cache/unswbc && chmod -R 777 /home/arena
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY --from=bcweb . /tmp/bcweb
RUN uv pip install --no-deps /tmp/bcweb && rm -rf /tmp/bcweb
COPY . .
RUN uv sync --locked --no-dev --inexact

# The judge's toolchain and the wasm build cache live in $HOME/.cache/unswbc; mount a volume
# there to keep it across runs (the engine does; a one-off Batch job prepares it once at start).
VOLUME ["/data", "/home/arena/.cache"]
EXPOSE 8700
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["arena", "serve", "--config", "/data/arena.toml", "--host", "127.0.0.1", "--port", "8700"]
