# The worker image for rented machines (docs/SCALER.md): only what `arena worker` needs, the
# official judge (unswbc, with the engine's wasmtime), cppjudge and Tailscale. No website, engine, MCP,
# bcweb or build tools. Built and pushed by deploy/update.sh:
#   docker build -f docker/worker.Dockerfile --build-arg UNSWBC=1.2.7 --build-arg WASMTIME=49.0.0 .

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

FROM python:3.12-slim AS tailscale
ARG TS_VERSION=1.102.4
ARG TARGETARCH=amd64
ADD https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_${TARGETARCH}.tgz /tmp/ts.tgz
RUN mkdir /ts && tar xzf /tmp/ts.tgz -C /ts --strip-components=1

FROM python:3.12-slim
ARG UNSWBC
ARG WASMTIME
COPY --from=tailscale /ts/tailscale /ts/tailscaled /usr/local/bin/
# The judge, pinned to the engine's versions (a different judge would be refused anyway), minus
# what only people at a desk use: the VS Code viewer and the project templates.
RUN pip install --no-cache-dir --no-compile "unswbc==${UNSWBC}" "wasmtime==${WASMTIME}" \
    && site=$(python -c "import unswbc, pathlib; print(pathlib.Path(unswbc.__file__).parent)") \
    && rm -rf "$site/replay-viewer.vsix" "$site/templates" \
    && find / -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null; true
# Only the worker's modules (all standard library): the rest of the arena isn't here.
COPY arena/__init__.py arena/cli.py arena/worker.py arena/judge.py arena/runner.py arena/strictjudge.py arena/stats.py \
     arena/state.py arena/store.py arena/config.py arena/maps.py arena/submit.py /app/arena/
COPY arena/metrics/__init__.py arena/metrics/state.py arena/metrics/native.py /app/arena/metrics/
COPY arena/metrics/plugins/ /app/arena/metrics/plugins/
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY --from=metrics /opt/arena-metrics /opt/arena-metrics
# cppjudge, loading the judge's own libwasmtime (the wheel's file: the C API release's is the same)
COPY --from=cppjudge /opt/cppjudge/cppjudge /opt/cppjudge/WASMTIME /opt/cppjudge/
COPY judge/unswbc-cpp /opt/cppjudge/unswbc-cpp
RUN python -c 'import importlib.metadata as m, sys; w = m.version("wasmtime"); sys.exit(0 if w == open("/opt/cppjudge/WASMTIME").read().strip() else f"cppjudge built for another wasmtime than {w}")' \
    && ln -sf "$(python -c 'import wasmtime, pathlib; print(pathlib.Path(wasmtime.__file__).parent / "linux-x86_64" / "_libwasmtime.so")')" \
        /opt/cppjudge/_libwasmtime.so && /opt/cppjudge/cppjudge 2>&1 | grep -q usage
RUN printf '#!/bin/sh\nexec python -m arena.cli "$@"\n' > /usr/local/bin/arena && chmod 755 /usr/local/bin/arena \
    && mkdir -p /home/arena/.cache/unswbc && chmod -R 777 /home/arena \
    && rm -rf /app/arena/metrics/plugins/__pycache__
ENV PYTHONPATH=/app HOME=/home/arena PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["worker"]
