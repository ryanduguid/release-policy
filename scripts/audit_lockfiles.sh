#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: Apache-2.0

# Audit Cargo lockfiles against a freshly fetched RustSec advisory database.
#
# CI runs this on pull requests and pushes (the change lane) and weekly (the
# scheduled lane); rustsec_lanes.sh says what each denies. Each lockfile gets
# its own fresh database under ADVISORY_DB, and every lockfile is audited
# before the script fails, so one run names every finding. As for the
# executables, there is no ignore list: the audits run in a new empty
# directory and refuse a Cargo home audit config.
set -euo pipefail

if [[ "$#" -lt 3 ]]; then
  echo "usage: audit_lockfiles.sh change|scheduled ABSOLUTE_ADVISORY_DB LOCKFILE..." >&2
  exit 64
fi

# shellcheck source=scripts/rustsec_lanes.sh
. "$(dirname "${BASH_SOURCE[0]}")/rustsec_lanes.sh"
lane="$1" db="$2"
shift 2
if [[ "$lane" != change && "$lane" != scheduled ]]; then
  echo "audit_lockfiles.sh audits the change or scheduled lane, not '$lane'" >&2
  exit 64
fi
lockfiles=()
for lockfile in "$@"; do
  if [[ ! -f "$lockfile" ]]; then
    echo "no such lockfile: $lockfile" >&2
    exit 64
  fi
  lockfiles+=("$(cd "$(dirname "$lockfile")" && pwd)/$(basename "$lockfile")")
done
mapfile -t deny < <(deny_arguments "$lane")
refuse_unsafe_setup "$db"
mkdir -p "$db"

work="$(mktemp -d)"
cd "$work"
failed=()
n=0
for lockfile in "${lockfiles[@]}"; do
  n=$((n + 1))
  if ! cargo-audit audit --db "$db/$n" "${deny[@]}" --file "$lockfile" 2>&1 | tee "audit-$n.log" \
      || ! require_yanked_check "audit-$n.log"; then
    failed+=("$lockfile")
  fi
done
if [[ "${#failed[@]}" -gt 0 ]]; then
  printf 'RustSec audit failed for %s\n' "${failed[@]}" >&2
  exit 1
fi
