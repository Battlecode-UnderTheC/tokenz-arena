#!/bin/sh
# Builds arena-metrics: build.sh [OUT] (default native/metrics/build/arena-metrics). Needs g++ 11+
# (or clang++ 14+) and zlib.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here/build/arena-metrics}
mkdir -p "$(dirname "$out")"
${CXX:-g++} -std=c++20 -O2 -Wall -Wextra -pthread -I"$here" "$here"/core.cc "$here"/main.cc "$here"/plugins/*.cc \
  ${STATIC:+-static-libstdc++ -static-libgcc} -lz -o "$out"
echo "built $out"
