# tokenz-arena: the engine and website (compose.yaml), and a self-contained remote judge:
#   docker run -e ARENA_WORKER_TOKEN=... -e TS_AUTHKEY=tskey-auth-... <image> worker
# (docs/WORKERS.md). The worker brings its own Tailscale (userspace: no root, no /dev/net/tun).
# bcweb (private, optional) comes in as a build context, not over the network:
#   --build-context bcweb=../bcweb  (compose.yaml sets it)
# cppjudge (judge/): the C++ host for the judge's wasm bots, against the wasmtime C API of the
# same release as the judge's wasmtime wheel; it loads the wheel's own libwasmtime at run time.
# arena-metrics (native/metrics): the metrics in C++. Only the binary leaves this stage.
FROM python:3.12-slim AS metrics
RUN apt-get update && apt-get install -y --no-install-recommends g++ zlib1g-dev && rm -rf /var/lib/apt/lists/*
COPY native/metrics /src
RUN STATIC=1 /src/build.sh /opt/arena-metrics/arena-metrics && /opt/arena-metrics/arena-metrics --describe > /dev/null

FROM python:3.12-slim AS cppjudge
ARG WASMTIME=49.0.0
RUN apt-get update && apt-get install -y --no-install-recommends g++ curl xz-utils ca-certificates \
    && curl -fsSL "https://github.com/bytecodealliance/wasmtime/releases/download/v${WASMTIME}/wasmtime-v${WASMTIME}-x86_64-linux-c-api.tar.xz" \
       | tar -xJ -C /opt && mv "/opt/wasmtime-v${WASMTIME}-x86_64-linux-c-api" /opt/wasmtime
COPY judge/src /src
RUN mkdir -p /opt/cppjudge && cp /opt/wasmtime/lib/libwasmtime.so /opt/cppjudge/_libwasmtime.so \
    && g++ -std=c++17 -O2 -I/opt/wasmtime/include /src/main.cc /src/sandbox.cc -static-libstdc++ -static-libgcc \
        -L/opt/cppjudge -l:_libwasmtime.so -lpthread -Wl,-rpath,'$ORIGIN' -o /opt/cppjudge/cppjudge \
    && echo "${WASMTIME}" > /opt/cppjudge/WASMTIME

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
COPY --from=metrics /opt/arena-metrics /opt/arena-metrics
# cppjudge, loading the judge's own libwasmtime (the wheel's file: the C API release's is the same)
COPY --from=cppjudge /opt/cppjudge/cppjudge /opt/cppjudge/WASMTIME /opt/cppjudge/
COPY judge/unswbc-cpp /opt/cppjudge/unswbc-cpp
RUN python -c 'import importlib.metadata as m, sys; w = m.version("wasmtime"); sys.exit(0 if w == open("/opt/cppjudge/WASMTIME").read().strip() else f"cppjudge built for another wasmtime than {w}")' \
    && ln -sf "$(python -c 'import wasmtime, pathlib; print(pathlib.Path(wasmtime.__file__).parent / "linux-x86_64" / "_libwasmtime.so")')" \
        /opt/cppjudge/_libwasmtime.so && /opt/cppjudge/cppjudge 2>&1 | grep -q usage

# The judge's toolchain and the wasm build cache live in $HOME/.cache/unswbc; mount a volume
# there to keep it across runs (the engine does; a one-off Batch job prepares it once at start).
VOLUME ["/data", "/home/arena/.cache"]
EXPOSE 8700
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["arena", "serve", "--config", "/data/arena.toml", "--host", "127.0.0.1", "--port", "8700"]
