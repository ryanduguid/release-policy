#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Audit shipped executables against a freshly fetched RustSec advisory database.
#
# cargo-audit reads the dependency list cargo-auditable embedded in each
# executable. A vulnerability, an unsound or yanked crate fails, and in the
# scheduled lane so does an unmaintained one (rustsec_lanes.sh); so does a
# failed database fetch or yanked check, or an executable whose embedded list
# cargo-audit did not read. There is no ignore list: the audit runs in a new
# empty directory, so no project .cargo/audit.toml applies, and it refuses to
# run when the Cargo home holds one.
set -euo pipefail

if [[ "$#" -lt 3 ]]; then
  echo "usage: audit_binaries.sh release|scheduled ADVISORY_DB ABSOLUTE_BINARY..." >&2
  exit 64
fi

# shellcheck source=scripts/rustsec_lanes.sh
. "$(dirname "${BASH_SOURCE[0]}")/rustsec_lanes.sh"
lane="$1" db="$2"
shift 2
if [[ "$lane" != release && "$lane" != scheduled ]]; then
  echo "audit_binaries.sh audits the release or scheduled lane, not '$lane'" >&2
  exit 64
fi
mapfile -t deny < <(deny_arguments "$lane")
refuse_unsafe_setup "$db"

work="$(mktemp -d)"
cd "$work"
cargo-audit audit --db "$db" "${deny[@]}" bin "$@" 2>&1 | tee audit.log
require_yanked_check audit.log
for binary in "$@"; do
  if ! grep -qF "Found 'cargo auditable' data in $binary (" audit.log; then
    echo "cargo-audit read no embedded dependency list from $binary" >&2
    exit 1
  fi
done
