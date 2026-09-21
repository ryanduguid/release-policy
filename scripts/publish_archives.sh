#!/usr/bin/env bash
# Publish one already-gated source-archive release without executing consumer code.
set -euo pipefail

if [ "$#" -ne 5 ]; then
  echo "usage: publish_archives.sh TAG EXPECTED_COMMIT STEM VERSION SOURCE_PATH" >&2
  exit 64
fi

tag="$1"
expected_commit="$2"
base="$3-$4"
source_path="$5"
dist="$source_path/dist"
zip="$dist/$base.zip"
tar="$dist/$base.tar.gz"
sbom="$dist/$base.spdx.json"
checksums="$dist/SHA256SUMS"
assets=("$zip" "$tar" "$sbom" "$checksums")

# shellcheck source=scripts/publish_common.sh
. "$(dirname "$0")/publish_common.sh"
trap cleanup_current_draft EXIT

create_draft
upload_asset "$zip" application/zip Deterministic%20source%20ZIP
upload_asset "$tar" application/gzip Deterministic%20source%20tar%20archive
upload_asset "$sbom" application/spdx+json SPDX%202.3%20SBOM
upload_asset "$checksums" text/plain SHA-256%20checksums
finalise_release
