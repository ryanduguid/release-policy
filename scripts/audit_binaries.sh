#!/usr/bin/env bash
# Audit shipped executables against a freshly fetched RustSec advisory database.
#
# cargo-audit reads the dependency list cargo-auditable embedded in each
# executable. Any advisory fails, informational ones such as an unmaintained
# crate included, and so does a failed database fetch or an executable whose
# embedded list cargo-audit did not read. There is no ignore list: the audit
# runs in a new empty directory, so no project .cargo/audit.toml applies, and
# it refuses to run when the Cargo home holds one.
set -euo pipefail

if [[ "$#" -lt 2 ]]; then
  echo "usage: audit_binaries.sh ADVISORY_DB ABSOLUTE_BINARY..." >&2
  exit 64
fi

db="$1"
shift
config="${CARGO_HOME:-$HOME/.cargo}/audit.toml"
if [[ -e "$db" ]]; then
  echo "the advisory database must be fetched fresh, but $db exists" >&2
  exit 1
fi
if [[ -e "$config" ]]; then
  echo "refusing to audit with $config, which could ignore advisories" >&2
  exit 1
fi

work="$(mktemp -d)"
cd "$work"
cargo-audit audit --db "$db" --no-yanked --deny warnings bin "$@" 2>&1 | tee audit.log
for binary in "$@"; do
  if ! grep -qF "Found 'cargo auditable' data in $binary (" audit.log; then
    echo "cargo-audit read no embedded dependency list from $binary" >&2
    exit 1
  fi
done
