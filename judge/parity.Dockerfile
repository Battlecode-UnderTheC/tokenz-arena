# The arena image plus cppjudge, for judge/parity.py. Build judge/Dockerfile first:
#   docker build -f judge/Dockerfile -t cppjudge-build .
#   docker build -f judge/parity.Dockerfile -t cppjudge-parity .
#   docker run --rm -v ~/tokenz-arena-data:/data:ro -v $PWD/work:/work cppjudge-parity \
#       python /opt/cppjudge/parity.py -n 60 -j 4 --out /work/parity.jsonl
FROM cppjudge-build AS build
FROM tokenz-arena:latest
COPY --from=build /opt/cppjudge /opt/cppjudge
# cppjudge loads the judge's own libwasmtime: the wheel's file, not a copy.
RUN ln -sf "$(python -c 'import wasmtime, pathlib; print(pathlib.Path(wasmtime.__file__).parent / "linux-x86_64" / "_libwasmtime.so")')" \
        /opt/cppjudge/_libwasmtime.so && /opt/cppjudge/cppjudge 2>&1 | grep -q usage
COPY judge/parity.py /opt/cppjudge/parity.py
ENTRYPOINT []
