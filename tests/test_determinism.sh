#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
