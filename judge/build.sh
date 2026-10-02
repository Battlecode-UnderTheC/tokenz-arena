#!/bin/sh
# Builds cppjudge for Linux x86_64 in Docker (judge/Dockerfile) and copies it to judge/build/.
#   judge/build.sh            -> judge/build/{cppjudge,_libwasmtime.so}
set -eu
here=$(cd "$(dirname "$0")" && pwd)
docker build -f "$here/Dockerfile" -t cppjudge-build "$here/.."
mkdir -p "$here/build"
id=$(docker create cppjudge-build)
trap 'docker rm -f "$id" >/dev/null' EXIT
docker cp "$id:/opt/cppjudge/cppjudge" "$here/build/cppjudge"
docker cp "$id:/opt/cppjudge/_libwasmtime.so" "$here/build/_libwasmtime.so"
echo "built $here/build/cppjudge"
