# Builds cppjudge for Linux x86_64 against the wasmtime 49 C API, linked to the very
# libwasmtime the judge's `wasmtime==49.0.0` wheel loads (the wheel's _libwasmtime.so is
# byte-identical to the v49.0.0 C API release's libwasmtime.so; the build checks that).
#   docker build -f judge/Dockerfile -t cppjudge-build .
# The result is /opt/cppjudge: cppjudge, unswbc-cpp, and _libwasmtime.so (a copy of the
# wheel's; images that have the wheel replace it with a symlink, see docker/worker.Dockerfile).
FROM python:3.12-slim
ARG WASMTIME=49.0.0
RUN apt-get update && apt-get install -y --no-install-recommends g++ curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --no-compile "wasmtime==${WASMTIME}" \
    && curl -fsSL "https://github.com/bytecodealliance/wasmtime/releases/download/v${WASMTIME}/wasmtime-v${WASMTIME}-x86_64-linux-c-api.tar.xz" \
       | tar -xJ -C /opt && mv "/opt/wasmtime-v${WASMTIME}-x86_64-linux-c-api" /opt/wasmtime \
    && wheel=$(python -c "import wasmtime, pathlib; print(pathlib.Path(wasmtime.__file__).parent / 'linux-x86_64' / '_libwasmtime.so')") \
    && cmp "$wheel" /opt/wasmtime/lib/libwasmtime.so \
    && mkdir -p /opt/cppjudge && cp "$wheel" /opt/cppjudge/_libwasmtime.so
COPY judge/src /src
COPY judge/unswbc-cpp /opt/cppjudge/unswbc-cpp
# The library has no SONAME, so it is linked by its file name and found through the rpath.
RUN g++ -std=c++17 -O2 -Wall -I/opt/wasmtime/include /src/main.cc /src/sandbox.cc \
        -L/opt/cppjudge -l:_libwasmtime.so -lpthread -Wl,-rpath,'$ORIGIN' -o /opt/cppjudge/cppjudge \
    && /opt/cppjudge/cppjudge 2>&1 | grep -q usage
