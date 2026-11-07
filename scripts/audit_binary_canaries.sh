#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Audit the executables of each binary canary's recorded release.
#
# CI's scheduled release-audit job runs this, so an advisory published after a
# release fails that week's run. For each binary entry in canaries.json it
# downloads the recorded release's executable archives, requires each to be
# the one line of SHA256SUMS that names it and to carry provenance from the
# provenance bundle that verifies against the repository, tag, source commit,
# signer workflow and policy commit the entry records, then extracts every
# executable. audit_binaries.sh audits them all against one freshly fetched
# advisory database. WORK_DIR must be absolute and must not exist.
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

executables=()
while IFS=$'\t' read -r -u 3 repository tag commit policy; do
  release="$work/${repository/\//-}"
  mkdir "$release"
  names="$(gh release view "$tag" --repo "$repository" --json assets --jq '.assets[].name')"
  archives=()
  while read -r name; do
    case "$name" in
      *-x86_64-unknown-linux-musl.tar.gz | *-aarch64-unknown-linux-musl.tar.gz \
        | *-aarch64-apple-darwin.tar.gz | *-x86_64-pc-windows-msvc.zip) archives+=("$name") ;;
    esac
  done <<< "$names"
  bundle="$(grep -E '\.intoto\.jsonl$' <<< "$names" || true)"
  if [[ "${#archives[@]}" -eq 0 || -z "$bundle" || "$bundle" == *$'\n'* ]]; then
    echo "$repository $tag: expected executable archives and one provenance bundle, found: $names" >&2
    exit 1
  fi
  patterns=(--pattern SHA256SUMS --pattern "$bundle")
  for archive in "${archives[@]}"; do
    patterns+=(--pattern "$archive")
  done
  gh release download "$tag" --repo "$repository" --dir "$release" "${patterns[@]}"
  cd "$release"
  for archive in "${archives[@]}"; do
    awk -v name="$archive" '$2 == name' SHA256SUMS > "$archive.sha256"
    test "$(wc -l < "$archive.sha256")" -eq 1
    sha256sum --check --strict "$archive.sha256"
    gh attestation verify "$archive" --repo "$repository" --bundle "$bundle" \
      --source-ref "refs/tags/$tag" --source-digest "$commit" \
      --signer-workflow "$signer" --signer-digest "$policy"
    extracted="$release/extracted/$archive"
    mkdir -p "$extracted"
    if [[ "$archive" == *.zip ]]; then
      unzip -q "$archive" -d "$extracted"
    else
      tar -xzf "$archive" -C "$extracted"
    fi
    # The executables, by their ELF, Mach-O or PE magic; the documents and
    # data files beside them are not audited.
    for file in "$extracted"/*; do
      case "$(od -An -tx1 -N4 "$file" | tr -d ' \n')" in
        7f454c46 | cffaedfe | 4d5a*) executables+=("$file") ;;
      esac
    done
  done
  cd "$work"
done 3<<< "$entries"

if [[ "${#executables[@]}" -eq 0 ]]; then
  echo "no executable found in the binary canaries' archives" >&2
  exit 1
fi
bash "$here/audit_binaries.sh" "$db" "${executables[@]}"
