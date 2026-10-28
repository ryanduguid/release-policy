#!/usr/bin/env bash
# Test, build, package and check one target of a Rust binary release.
#
# Runs in a read-only build job of release-binary.yml, and in CI's self-check
# on this repository, so both exercise the same steps. It executes consumer
# code (tests and build scripts) and must never run where a write token is.
# The caller installs cargo-auditable and, for musl, musl-tools; the Windows
# job sets RUSTFLAGS for the static C runtime.
set -euo pipefail

if [[ "$#" -ne 8 ]]; then
  echo "usage: build_binary.sh TARGET STEM VERSION BINARY SOURCE_PATH ROOT OUTPUT_DIR EPOCH" >&2
  exit 64
fi

target="$1" stem="$2" version="$3" binary="$4" source_path="$5" root="$6" output_dir="$7" epoch="$8"
PYTHON="${PYTHON:-python3}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
release=("$PYTHON" "$here/binary_release.py")

case "$target" in
  x86_64-unknown-linux-musl) suffix="" ;;
  x86_64-pc-windows-msvc) suffix=".exe" ;;
  *) echo "build_binary.sh: unsupported target $target" >&2; exit 64 ;;
esac
[[ "$epoch" =~ ^[0-9]+$ ]] || { echo "build_binary.sh: epoch must be a Unix time" >&2; exit 64; }

cd "$source_path"
"${release[@]}" toolchain --source . --root "$root"
# rustup reads the toolchain file the check above accepted.
rustup toolchain install
rustup target add "$target"
rustup component add llvm-tools

cargo test --locked --target "$target"
cargo auditable build --locked --release --target "$target" --bin "$binary"
target_directory="$(cargo metadata --locked --format-version 1 --no-deps \
  | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')"
executable="$target_directory/$target/release/$binary$suffix"

mkdir -p "$output_dir"
archive="$("${release[@]}" package --executable "$executable" --binary "$binary" \
  --target "$target" --stem "$stem" --version "$version" --source . --root "$root" \
  --output-dir "$output_dir" --epoch "$epoch" | tr -d '\r')"

# Check what ships: the packaged executable, extracted afresh, not the build output.
check="$(mktemp -d)"
"${release[@]}" verify --archive "$archive" --binary "$binary" --target "$target" \
  --extract-dir "$check/extracted" >/dev/null
"${release[@]}" smoke --executable "$check/extracted/$binary$suffix" --version "$version"

host="$(rustc -vV | sed -n 's/^host: //p' | tr -d '\r')"
readobj="$(rustc --print sysroot | tr -d '\r')/lib/rustlib/$host/bin/llvm-readobj"
if [[ -z "$suffix" ]]; then
  "$readobj" --file-headers --needed-libs --program-headers "$check/extracted/$binary" > "$check/readobj.txt"
  "${release[@]}" linkage --readobj-output "$check/readobj.txt" --target "$target"
else
  "$readobj.exe" --file-headers --needed-libs "$check/extracted/$binary.exe" > "$check/readobj.txt"
  "${release[@]}" linkage --readobj-output "$check/readobj.txt" --target "$target" \
    --system-directory "${SYSTEMROOT:-C:\\Windows}\\System32"
fi

(cd "$output_dir" && sha256sum -- "${archive##*/}" > "${archive##*/}.sha256")
echo "$archive"
