#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Publish one already-gated source-archive release without executing consumer code.
set -euo pipefail

if [[ "$#" -ne 6 ]]; then
  echo "usage: publish_archives.sh TAG EXPECTED_COMMIT STEM VERSION SOURCE_PATH SOURCE_DIRECTORY" >&2
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
checksums="$dist/SHA256SUMS"
# The signed provenance bundle the workflow wrote after attestation.
# SHA256SUMS cannot list it: the attestation covers that file.
provenance="$dist/$base.intoto.jsonl"
# Skill upload ZIPs, present only when the caller lists skill-archives;
# the workflow has already checked the directory holds exactly the expected set.
shopt -s nullglob
skill_zips=("$dist/$base"-*.zip)
shopt -u nullglob
assets=("$zip" "$tar" "$sbom" "$checksums" "$provenance" "${skill_zips[@]}")

# shellcheck source=scripts/publish_common.sh
. "$(dirname "$0")/publish_common.sh"
trap cleanup_current_draft EXIT

create_draft
upload_asset "$zip" application/zip Deterministic%20source%20ZIP
upload_asset "$tar" application/gzip Deterministic%20source%20tar%20archive
upload_asset "$sbom" application/spdx+json SPDX%202.3%20SBOM
upload_asset "$checksums" text/plain SHA-256%20checksums
upload_asset "$provenance" application/jsonl SLSA%20provenance%20bundle
for skill_zip in "${skill_zips[@]}"; do
  skill_name="${skill_zip#"$dist/$base"-}"
  # No skill can be named skills.bundle: names are lower-case and hyphenated.
  if [[ "$skill_name" == skills.bundle.zip ]]; then
    upload_asset "$skill_zip" application/zip "Bundle%20of%20skill%20upload%20ZIPs"
  else
    upload_asset "$skill_zip" application/zip "Upload%20ZIP%20for%20skill%20${skill_name%.zip}"
  fi
done
finalise_release
write_release_summary publish-archives
