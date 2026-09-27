#!/usr/bin/env bash
# The release build installs nothing: once the locked dev extra is synced, the
# demo package builds its wheel and sdist without isolation and with every
# index disabled, as release-python.yml builds a consumer.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/../fixtures/demo-pkg"

work="$(mktemp -d)"
cleanup() {
  rm -rf -- "$work" build demo_pkg.egg-info
}
trap cleanup EXIT
export UV_PROJECT_ENVIRONMENT="$work/venv"

uv sync --locked --extra dev --quiet
UV_OFFLINE=1 PIP_NO_INDEX=1 uv run --locked --extra dev \
  python -m build --no-isolation --outdir "$work/dist" >/dev/null
test -f "$work/dist/demo_pkg-0.1.0-py3-none-any.whl"
test -f "$work/dist/demo_pkg-0.1.0.tar.gz"
echo "offline build from the locked dev extra confirmed"
