#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Test, build, package and check one target of a Rust binary release.
#
# Runs in a read-only build job of release-binary.yml, and in CI's self-check
# on this repository, so both exercise the same steps. It executes consumer
# code (tests and build scripts) and must never run where a write token is.
# The caller installs cargo-auditable and, for musl, musl-tools; the Windows
# job sets RUSTFLAGS for the static C runtime. BINARY names one or more Cargo
# binary targets separated by spaces; DATA_FILES lists the files they need,
# one path per line relative to the component, or is empty. Written for the
# bash 3.2 that macOS ships.
set -euo pipefail

if [[ "$#" -ne 9 ]]; then
  echo "usage: build_binary.sh TARGET STEM VERSION BINARY SOURCE_PATH ROOT OUTPUT_DIR EPOCH DATA_FILES" >&2
  exit 64
fi

target="$1" stem="$2" version="$3" binary="$4" source_path="$5" root="$6" output_dir="$7" epoch="$8" data_files="$9"
PYTHON="${PYTHON:-python3}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
release=("$PYTHON" "$here/binary_release.py")

# llvm-readobj prints an ELF executable's program headers, which show any
# interpreter; a Windows executable names its DLLs in the needed list. A macOS
# executable's load commands are read directly (binary_release.py says why).
case "$target" in
  x86_64-unknown-linux-musl | aarch64-unknown-linux-musl)
    suffix="" extension=".tar.gz" headers=(--program-headers) ;;
  aarch64-apple-darwin) suffix="" extension=".tar.gz" ;;
  x86_64-pc-windows-msvc) suffix=".exe" extension=".zip" headers=() ;;
  *) echo "build_binary.sh: unsupported target $target" >&2; exit 64 ;;
esac
[[ "$epoch" =~ ^[0-9]+$ ]] || { echo "build_binary.sh: epoch must be a Unix time" >&2; exit 64; }
# The release workflow has already checked each name against Cargo.toml.
read -r -a names <<< "$binary"
bins=()
for name in "${names[@]}"; do
  bins+=(--bin "$name")
done

cd "$source_path"
"${release[@]}" toolchain --source . --root "$root"
# rustup reads the toolchain file the check above accepted.
rustup toolchain install
rustup target add "$target"
rustup component add llvm-tools

cargo test --locked --target "$target"
cargo auditable build --locked --release --target "$target" "${bins[@]}"
target_directory="$(cargo metadata --locked --format-version 1 --no-deps \
  | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')"

mkdir -p "$output_dir"
# Python prints a native path, with backslashes on Windows, so the name the
# digest line records is built here rather than cut from that output.
name="$stem-$version-$target$extension"
"${release[@]}" package --executables "$target_directory/$target/release" --binary "$binary" \
  --target "$target" --stem "$stem" --version "$version" --source . --root "$root" \
  --output-dir "$output_dir" --epoch "$epoch" --data-files "$data_files" >/dev/null
archive="$output_dir/$name"

# Check what ships: the packaged executables, extracted afresh, not the build output.
check="$(mktemp -d)"
"${release[@]}" verify --archive "$archive" --binary "$binary" --target "$target" \
  --extract-dir "$check/extracted" --data-files "$data_files" >/dev/null

host="$(rustc -vV | sed -n 's/^host: //p' | tr -d '\r')"
readobj="$(rustc --print sysroot | tr -d '\r')/lib/rustlib/$host/bin/llvm-readobj"
[[ -z "$suffix" ]] || readobj="$readobj.exe"
for executable in "${names[@]}"; do
  executable="$check/extracted/$executable$suffix"
  "${release[@]}" smoke --executable "$executable" --version "$version"
  if [[ "$target" == *-apple-darwin ]]; then
    "${release[@]}" macho-linkage --executable "$executable" --target "$target"
  else
    "$readobj" --file-headers --needed-libs ${headers[@]+"${headers[@]}"} "$executable" > "$check/readobj.txt"
    "${release[@]}" linkage --readobj-output "$check/readobj.txt" --target "$target"
  fi
done

# macOS ships shasum rather than sha256sum; both print the same line.
if command -v sha256sum >/dev/null; then
  (cd "$output_dir" && sha256sum -- "$name" > "$name.sha256")
else
  (cd "$output_dir" && shasum -a 256 -- "$name" > "$name.sha256")
fi
echo "$archive"
