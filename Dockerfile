# The arena: API, website, MCP, judge workers and server bridge in one process.
#   docker compose up -d            (compose.yaml: with a Tailscale sidecar)
# bcweb (private, optional) comes in as a build context, not over the network:
#   --build-context bcweb=../bcweb  (compose.yaml sets it)
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.* && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
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

# The judge's toolchain and the wasm build cache live in $HOME/.cache/unswbc (a bind mount).
VOLUME ["/data", "/home/arena/.cache/unswbc"]
EXPOSE 8700
CMD ["arena", "serve", "--config", "/data/arena.toml", "--host", "127.0.0.1", "--port", "8700"]
