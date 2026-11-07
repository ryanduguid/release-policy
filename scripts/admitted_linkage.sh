#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Check the linkage of every admitted executable in the release's admit job.
#
# The build jobs run the same check, but they also ran consumer code, which
# could replace an archive after its own check. This runs on the bytes the
# admit job admitted, which the publisher admits again and requires to be the
# same, with llvm-readobj from a toolchain pinned here rather than by the
# consumer, and from outside the consumer tree. EXTRACTED_ROOT holds one
# directory for each TARGET; BINARY names the executables, separated by spaces.
set -euo pipefail

if [[ "$#" -lt 3 ]]; then
  echo "usage: admitted_linkage.sh EXTRACTED_ROOT BINARY TARGET..." >&2
  exit 64
fi

extracted="$1"
read -r -a names <<< "$2"
shift 2
PYTHON="${PYTHON:-python3}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
toolchain=1.99.1
work="$(mktemp -d)"

cd "$work"
rustup toolchain install "$toolchain" --profile minimal --component llvm-tools --no-self-update >&2
host="$(rustc "+$toolchain" -vV | sed -n 's/^host: //p' | tr -d '\r')"
readobj="$(rustc "+$toolchain" --print sysroot | tr -d '\r')/lib/rustlib/$host/bin/llvm-readobj"

for target in "$@"; do
  # As in build_binary.sh: an ELF executable's program headers show any interpreter.
  case "$target" in
    *-linux-musl) suffix="" headers=(--program-headers) ;;
    *-apple-darwin) suffix="" headers=() ;;
    *-windows-msvc) suffix=".exe" headers=() ;;
    *) echo "admitted_linkage.sh: unsupported target $target" >&2; exit 64 ;;
  esac
  for name in "${names[@]}"; do
    "$readobj" --file-headers --needed-libs ${headers[@]+"${headers[@]}"} \
      "$extracted/$target/$name$suffix" > "$work/readobj.txt"
    "$PYTHON" "$here/binary_release.py" linkage --readobj-output "$work/readobj.txt" \
      --target "$target"
  done
done
