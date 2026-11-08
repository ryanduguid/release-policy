#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Audit the executables of each binary canary's recorded release.
#
# CI's scheduled release-audit job runs this, so an advisory published after a
# release fails that week's run. Each binary entry in canaries.json must name
# its repository, version tag, source commit and policy commit in full before
# GitHub is asked anything. For each entry it downloads the recorded release's
# SHA256SUMS and verifies its provenance against those 4 pins and the signer
# workflow, then takes the executable archives from the lines of that signed
# file, not from the release's asset listing, so none can be left out. Each
# archive must be the one line of SHA256SUMS that names it, carry the same
# provenance and hold at least one executable. audit_binaries.sh audits them
# all against one freshly fetched advisory database. WORK_DIR must be absolute
# and must not exist.
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: audit_binary_canaries.sh CANARIES_JSON WORK_DIR ADVISORY_DB" >&2
  exit 64
fi

manifest="$1" work="$2" db="$3"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
signer="ryanduguid/release-policy/.github/workflows/release-binary.yml"
[[ "$work" = /* ]] || { echo "WORK_DIR must be absolute: $work" >&2; exit 64; }
mkdir "$work"

entries="$(jq -r '.families[] | select(.family == "binary")
  | [.repository, .evidence.ref, .evidence.head_sha, .evidence.policy_sha] | @tsv' "$manifest")"
if [[ -z "$entries" ]]; then
  echo "$manifest records no binary canary" >&2
  exit 1
fi
# jq prints a missing field as an empty one, and gh may skip an empty pin.
while IFS=$'\t' read -r repository tag commit policy; do
  if ! [[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ \
      && "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ \
      && "$commit" =~ ^[0-9a-f]{40}$ && "$policy" =~ ^[0-9a-f]{40}$ ]]; then
    echo "$manifest has a malformed binary canary: repository '$repository', tag '$tag'," \
      "source commit '$commit', policy commit '$policy'" >&2
    exit 1
  fi
done <<< "$entries"

executables=()
while IFS=$'\t' read -r -u 3 repository tag commit policy; do
  release="$work/${repository/\//-}"
  mkdir "$release"
  names="$(gh release view "$tag" --repo "$repository" --json assets --jq '.assets[].name')"
  bundle="$(grep -E '\.intoto\.jsonl$' <<< "$names" || true)"
  if [[ -z "$bundle" || "$bundle" == *$'\n'* ]]; then
    echo "$repository $tag: expected one provenance bundle, found: $names" >&2
    exit 1
  fi
  pins=(--repo "$repository" --bundle "$bundle" --source-ref "refs/tags/$tag"
        --source-digest "$commit" --signer-workflow "$signer" --signer-digest "$policy")
  gh release download "$tag" --repo "$repository" --dir "$release" \
    --pattern SHA256SUMS --pattern "$bundle"
  cd "$release"
  gh attestation verify SHA256SUMS "${pins[@]}"
  archives=()
  while read -r _ name; do
    case "$name" in
      *-x86_64-unknown-linux-musl.tar.gz | *-aarch64-unknown-linux-musl.tar.gz \
        | *-aarch64-apple-darwin.tar.gz | *-x86_64-pc-windows-msvc.zip) archives+=("$name") ;;
    esac
  done < SHA256SUMS
  if [[ "${#archives[@]}" -eq 0 ]]; then
    echo "$repository $tag: SHA256SUMS lists no executable archive" >&2
    exit 1
  fi
  patterns=()
  for archive in "${archives[@]}"; do
    patterns+=(--pattern "$archive")
  done
  gh release download "$tag" --repo "$repository" --dir "$release" "${patterns[@]}"
  for archive in "${archives[@]}"; do
    awk -v name="$archive" '$2 == name' SHA256SUMS > "$archive.sha256"
    test "$(wc -l < "$archive.sha256")" -eq 1
    sha256sum --check --strict "$archive.sha256"
    gh attestation verify "$archive" "${pins[@]}"
    extracted="$release/extracted/$archive"
    mkdir -p "$extracted"
    if [[ "$archive" == *.zip ]]; then
      unzip -q "$archive" -d "$extracted"
    else
      tar -xzf "$archive" -C "$extracted"
    fi
    # The executables, by their ELF, Mach-O or PE magic; the documents and
    # data files beside them are not audited.
    found=0
    for file in "$extracted"/*; do
      case "$(od -An -tx1 -N4 "$file" | tr -d ' \n')" in
        7f454c46 | cffaedfe | 4d5a*) executables+=("$file"); found=$((found + 1)) ;;
      esac
    done
    if [[ "$found" -eq 0 ]]; then
      echo "$repository $tag: no executable in $archive" >&2
      exit 1
    fi
  done
  cd "$work"
done 3<<< "$entries"

bash "$here/audit_binaries.sh" "$db" "${executables[@]}"
