#!/usr/bin/env bash
# Publish one already-gated Python candidate without executing consumer code.
set -euo pipefail

if [ "$#" -ne 5 ]; then
  echo "usage: publish_python.sh TAG EXPECTED_COMMIT STEM VERSION SOURCE_PATH" >&2
  exit 64
fi

tag="$1"
expected_commit="$2"
base="$3-$4"
source_path="$5"
wheel="candidate/$base-py3-none-any.whl"
sdist="candidate/$base.tar.gz"
sbom="candidate/$base.spdx.json"
manifest="candidate/release-manifest.json"
checksums="candidate/SHA256SUMS"
assets=("$wheel" "$sdist" "$sbom" "$manifest" "$checksums")

# shellcheck source=scripts/publish_common.sh
. "$(dirname "$0")/publish_common.sh"
trap cleanup_current_draft EXIT

create_draft
upload_asset "$wheel" application/zip
upload_asset "$sdist" application/gzip
upload_asset "$sbom" application/spdx+json
upload_asset "$manifest" application/json
upload_asset "$checksums" text/plain
finalise_release
