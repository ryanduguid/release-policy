#!/usr/bin/env bash
# The release build installs nothing: once the locked dev extra is synced as
# wheels, with the project itself left uninstalled and every build refused, a
# package builds its wheel and sdist without isolation and with every index
# disabled, as release-python.yml builds a consumer. Two fixtures: a standalone
# setuptools package, and a hatchling member of a virtual workspace root, where
# the sync must pick the member's dev extra from its own directory.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

work="$(mktemp -d)"
cleanup() {
  rm -rf -- "$work" "$HERE/../fixtures/demo-pkg/build" "$HERE/../fixtures/demo-pkg/demo_pkg.egg-info"
}
trap cleanup EXIT

build_offline() {
  local directory="$1" stem="$2"
  (
    cd "$HERE/../fixtures/$directory"
    export UV_PROJECT_ENVIRONMENT="$work/$stem-venv"
    uv sync --locked --no-install-workspace --no-build --extra dev --quiet
    UV_OFFLINE=1 PIP_NO_INDEX=1 uv run --no-sync \
      python -m build --no-isolation --outdir "$work/$stem" >/dev/null
  )
  test -f "$work/$stem/$stem-0.1.0-py3-none-any.whl"
  test -f "$work/$stem/$stem-0.1.0.tar.gz"
}

build_offline demo-pkg demo_pkg
build_offline demo-workspace/packages/demo-member demo_member
echo "offline builds from the locked dev extra confirmed"
