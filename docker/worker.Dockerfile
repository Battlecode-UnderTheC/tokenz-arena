# The worker image for rented machines (docs/SCALER.md): only what `arena worker` needs, the
# official judge (unswbc, with the engine's wasmtime) and Tailscale. No website, engine, MCP,
# bcweb or build tools. Built and pushed by deploy/update.sh:
#   docker build -f docker/worker.Dockerfile --build-arg UNSWBC=1.2.7 --build-arg WASMTIME=49.0.0 .

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
COPY arena/__init__.py arena/cli.py arena/worker.py arena/judge.py arena/runner.py arena/stats.py \
     arena/state.py arena/store.py arena/config.py arena/maps.py arena/submit.py /app/arena/
COPY arena/metrics/__init__.py arena/metrics/state.py /app/arena/metrics/
COPY arena/metrics/plugins/ /app/arena/metrics/plugins/
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN printf '#!/bin/sh\nexec python -m arena.cli "$@"\n' > /usr/local/bin/arena && chmod 755 /usr/local/bin/arena \
    && mkdir -p /home/arena/.cache/unswbc && chmod -R 777 /home/arena \
    && rm -rf /app/arena/metrics/plugins/__pycache__
ENV PYTHONPATH=/app HOME=/home/arena PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["worker"]
