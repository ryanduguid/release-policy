#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: Apache-2.0

# What each RustSec audit lane denies, and the checks every audit shares.
# Sourced by audit_binaries.sh and audit_lockfiles.sh.
#
# cargo-audit fails on any vulnerability whatever it is told to deny. The
# change lane (pull requests and pushes) and the release lane also deny
# unsound and yanked crates. Only the scheduled lane denies unmaintained ones,
# so a crate going unmaintained fails that week's run without blocking a
# change or a release (Ryan, 18 November 2026).

# Print lane $1's --deny arguments, one per line.
deny_arguments() {
  case "$1" in
    change | release) printf '%s\n' --deny unsound --deny yanked ;;
    scheduled) printf '%s\n' --deny warnings ;;
    *)
      echo "unknown audit lane '$1': expected change, release or scheduled" >&2
      return 64
      ;;
  esac
}

# Refuse a database that is not fetched fresh, or a Cargo home audit config,
# which could ignore advisories. Each audit runs in a new empty directory, so
# no project .cargo/audit.toml applies.
refuse_unsafe_setup() {
  local config="${CARGO_HOME:-$HOME/.cargo}/audit.toml"
  if [[ -e "$1" ]]; then
    echo "the advisory database must be fetched fresh, but $1 exists" >&2
    return 1
  fi
  if [[ -e "$config" ]]; then
    echo "refusing to audit with $config, which could ignore advisories" >&2
    return 1
  fi
}

# cargo-audit only warns when it cannot reach the crates.io index, then skips
# the yanked check; fail instead, as the check was denied.
require_yanked_check() {
  if grep -qE "couldn't (update crates\.io index|check if the package is yanked)" "$1"; then
    echo "cargo-audit could not check for yanked crates" >&2
    return 1
  fi
}
