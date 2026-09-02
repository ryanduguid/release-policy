#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

cd "$HERE/../fixtures/demo-pkg"
rm -rf dist-a dist-b
PYTHONHASHSEED=0 SOURCE_DATE_EPOCH=1234567890 "$PYTHON" -m build --wheel --outdir dist-a >/dev/null
PYTHONHASHSEED=0 SOURCE_DATE_EPOCH=1234567890 "$PYTHON" -m build --wheel --outdir dist-b >/dev/null
sum_a="$(sha256sum dist-a/*.whl | awk '{print $1}')"
sum_b="$(sha256sum dist-b/*.whl | awk '{print $1}')"
rm -rf dist-a dist-b demo_pkg.egg-info build
if [ "$sum_a" != "$sum_b" ]; then
  echo "determinism failure: $sum_a != $sum_b" >&2
  exit 1
fi
echo "deterministic wheel build confirmed: $sum_a"

component_temp="$(mktemp -d "$ROOT/.component-determinism.XXXXXX")"
cleanup_component_archives() {
  rm -rf -- "$component_temp"
}
trap cleanup_component_archives EXIT
cd "$ROOT"
"$PYTHON" scripts/build_release_archives.py \
  --commit HEAD \
  --prefix demo-pkg-component/ \
  --source-directory fixtures/demo-pkg \
  --output-base "$component_temp/first/demo-pkg-component"
"$PYTHON" scripts/build_release_archives.py \
  --commit HEAD \
  --prefix demo-pkg-component/ \
  --source-directory fixtures/demo-pkg \
  --output-base "$component_temp/second/demo-pkg-component"
for suffix in .zip .tar.gz; do
  first_sum="$(sha256sum "$component_temp/first/demo-pkg-component$suffix" | awk '{print $1}')"
  second_sum="$(sha256sum "$component_temp/second/demo-pkg-component$suffix" | awk '{print $1}')"
  if [ "$first_sum" != "$second_sum" ]; then
    echo "component archive determinism failure: $first_sum != $second_sum" >&2
    exit 1
  fi
  echo "deterministic component archive confirmed ($suffix): $first_sum"
done
