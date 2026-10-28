#!/usr/bin/env bash
# Publish one already-gated Rust binary release without executing consumer code.
set -euo pipefail

if [[ "$#" -ne 6 ]]; then
  echo "usage: publish_binaries.sh TAG EXPECTED_COMMIT STEM VERSION SOURCE_PATH SOURCE_DIRECTORY" >&2
  exit 64
fi

tag="$1"
expected_commit="$2"
base="$3-$4"
source_path="$5"
source_directory="$6"
dist="$source_path/dist"
zip="$dist/$base.zip"
tar="$dist/$base.tar.gz"
sbom="$dist/$base.spdx.json"
linux="$dist/$base-x86_64-unknown-linux-musl"
windows="$dist/$base-x86_64-pc-windows-msvc"
checksums="$dist/SHA256SUMS"
# The signed provenance bundle the workflow wrote after attestation.
# SHA256SUMS cannot list it: the attestation covers that file.
provenance="$dist/$base.intoto.jsonl"
assets=(
  "$zip" "$tar" "$sbom"
  "$linux.tar.gz" "$linux.spdx.json"
  "$windows.zip" "$windows.spdx.json"
  "$checksums" "$provenance"
)

# shellcheck source=scripts/publish_common.sh
. "$(dirname "$0")/publish_common.sh"
trap cleanup_current_draft EXIT

create_draft
upload_asset "$zip" application/zip Deterministic%20source%20ZIP
upload_asset "$tar" application/gzip Deterministic%20source%20tar%20archive
upload_asset "$sbom" application/spdx+json SPDX%202.3%20source%20SBOM
upload_asset "$linux.tar.gz" application/gzip Linux%20x86_64%20musl%20executable
upload_asset "$linux.spdx.json" application/spdx+json SPDX%202.3%20Linux%20executable%20SBOM
upload_asset "$windows.zip" application/zip Windows%20x86_64%20executable
upload_asset "$windows.spdx.json" application/spdx+json SPDX%202.3%20Windows%20executable%20SBOM
upload_asset "$checksums" text/plain SHA-256%20checksums
upload_asset "$provenance" application/jsonl SLSA%20provenance%20bundle
finalise_release
