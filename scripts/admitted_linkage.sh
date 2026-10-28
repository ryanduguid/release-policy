#!/usr/bin/env bash
# Check the linkage of both admitted executables in the publishing job.
#
# The build jobs run the same check, but they also ran consumer code, which
# could replace an archive after its own check. This runs on the bytes the
# publisher admitted, with llvm-readobj from a toolchain pinned here rather
# than by the consumer, and from outside the consumer tree.
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: admitted_linkage.sh EXTRACTED_ROOT BINARY" >&2
  exit 64
fi

extracted="$1" binary="$2"
PYTHON="${PYTHON:-python3}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
toolchain=1.99.1
work="$(mktemp -d)"

cd "$work"
rustup toolchain install "$toolchain" --profile minimal --component llvm-tools --no-self-update >&2
host="$(rustc "+$toolchain" -vV | sed -n 's/^host: //p' | tr -d '\r')"
readobj="$(rustc "+$toolchain" --print sysroot | tr -d '\r')/lib/rustlib/$host/bin/llvm-readobj"

"$readobj" --file-headers --needed-libs --program-headers \
  "$extracted/x86_64-unknown-linux-musl/$binary" > "$work/linux.txt"
"$PYTHON" "$here/binary_release.py" linkage --readobj-output "$work/linux.txt" \
  --target x86_64-unknown-linux-musl
"$readobj" --file-headers --needed-libs \
  "$extracted/x86_64-pc-windows-msvc/$binary.exe" > "$work/windows.txt"
"$PYTHON" "$here/binary_release.py" linkage --readobj-output "$work/windows.txt" \
  --target x86_64-pc-windows-msvc
