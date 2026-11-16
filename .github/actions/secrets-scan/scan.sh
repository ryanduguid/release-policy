#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# The secrets scan behind action.yml. A push scans the commits it adds, a pull
# request the commits between its base and its head, and a scheduled or manual
# run the full history.
#
# Every scan reads its configuration from git objects, never from the work
# tree: betterleaks runs in an empty folder that holds one named commit's
# .gitleaks.toml and .gitleaksignore and this action's own rules.toml, with
# GIT_DIR pointing it at the checkout's history. So no scan reads a
# .gitattributes, a .betterleaks.toml or a .betterleaksignore, and the shared
# rules always come from the pinned action, not from the caller's mirror.
#
# A pull request is scanned twice, on pull_request or pull_request_target.
# The event scan reads the pull request's own configuration (the merge
# commit's, or the head's on pull_request_target), which proves a changed
# .gitleaks.toml still loads: the gate
# reads the base's copy, so a broken one that reached the base would fail every
# later pull request, its own fix included. The gate reads the base commit's
# .gitleaks.toml and .gitleaksignore and ignores gitleaks:allow and
# betterleaks:allow comments, so a pull request cannot allowlist what it adds.
# A new allowlist entry or fingerprint must reach the base first, in its own
# pull request.
#
# --diff-merges=remerge shows what a two-parent merge adds beyond its parents.
# Git shows nothing for a merge of three or more parents, so a push or pull
# request holding one fails, as does a pull request range holding no commit.
# --text makes Git show every file as text: without it one NUL byte in a file
# turns its diff into 'Binary files differ' and hides the rest from the scan.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rules="$here/rules.toml"
# Where a caller's .gitleaks.toml expects the shared rules ([extend] path).
mirror=.github/secrets-rules.toml
git_dir="$(git rev-parse --absolute-git-dir)"
work="$(mktemp -d "${RUNNER_TEMP:?}/secrets-scan.XXXXXX")"

error() {
  echo "::error::$1"
}

refuse_octopus() { # <range>
  local octopus
  octopus="$(git rev-list --min-parents=3 "$1")"
  if [ -n "$octopus" ]; then
    error "A merge of three or more parents cannot be scanned for what it adds: ${octopus//$'\n'/ }"
    exit 1
  fi
}

# The caller's copy exists for local hooks and scans, which need the file the
# caller's .gitleaks.toml extends. It must equal the rules this action runs.
check_mirror() { # <commit>
  if ! git show "$1:$mirror" 2> /dev/null | cmp -s - "$rules"; then
    error "$mirror in $1 is missing or differs from the shared rules. Copy .github/actions/secrets-scan/rules.toml from the pinned ryanduguid/release-policy commit to $mirror."
    return 1
  fi
}

# Scans with <commit>'s configuration. Every step is checked by hand because
# the callers below test this function's status, which turns errexit off in it.
scan() { # <label> <commit> [betterleaks git arguments]
  local label=$1 commit=$2 folder="$work/$1" canary=0 ignore
  shift 2
  mkdir -p "$folder/.github" || return 1
  if ! git show "$commit:.gitleaks.toml" > "$folder/.gitleaks.toml"; then
    error "Commit $commit has no .gitleaks.toml, so the $label scan has no reviewed configuration to scan with."
    return 1
  fi
  # A commit without .gitleaksignore leaves every finding in force. ls-tree
  # lists nothing for an absent file and fails only when Git itself does.
  ignore="$(git ls-tree --name-only "$commit" -- .gitleaksignore)" || return 1
  if [ -n "$ignore" ]; then
    git show "$commit:.gitleaksignore" > "$folder/.gitleaksignore" || return 1
  fi
  cp "$rules" "$folder/$mirror" || return 1
  # A configuration that stopped extending the shared rules would still load
  # and pass. This fabricated ABN fails the ABN checksum; the composed
  # configuration must report it. Exit 42 is a finding; 1 is a failed load.
  (cd "$folder" && printf 'Supplier ABN: 98 765 432 100\n' | betterleaks stdin \
    --config .gitleaks.toml --redact --no-banner --exit-code 42 > "$work/$label-canary.log" 2>&1) || canary=$?
  if [ "$canary" -ne 42 ]; then
    cat "$work/$label-canary.log"
    error "The .gitleaks.toml of commit $commit does not load with the shared rules, or does not report a labelled ABN. It must hold [extend] path = \"$mirror\" and no allowlist for the fabricated ABN 98 765 432 100."
    return 1
  fi
  # betterleaks reads the .gitleaksignore in its source folder, which holds
  # only <commit>'s.
  (cd "$folder" && GIT_DIR="$git_dir" betterleaks git . \
    --config .gitleaks.toml --redact --verbose --no-banner "$@")
}

status=0
case "${EVENT_NAME:?}" in
  pull_request | pull_request_target)
    range="${BASE_SHA:?}..${HEAD_SHA:?}"
    if [ -z "$(git rev-list -n 1 "$range")" ]; then
      error "No commit lies between the base and the head, so nothing would be scanned."
      exit 1
    fi
    refuse_octopus "$range"
    check_mirror "${CONFIG_SHA:?}" || status=1
    scan event "$CONFIG_SHA" --log-opts="--text --diff-merges=remerge $range" || status=1
    if ! scan gate "$BASE_SHA" --ignore-gitleaks-allow --log-opts="--text --diff-merges=remerge $range"; then
      status=1
      # Only names the files; their changes still cannot pass the gate.
      changed="$(git diff --name-only "$BASE_SHA...$HEAD_SHA" -- .gitleaks.toml .gitleaksignore || true)"
      if [ -n "$changed" ]; then
        error "This pull request changes ${changed//$'\n'/ and }, which may be why the gate failed: it uses the base's copy, so land that change in its own pull request first."
      fi
    fi
    ;;
  push)
    range="${BASE_SHA:?}..${HEAD_SHA:?}"
    refuse_octopus "$range"
    check_mirror "${CONFIG_SHA:?}" || status=1
    scan event "$CONFIG_SHA" --log-opts="--text --diff-merges=remerge $range" || status=1
    ;;
  schedule | workflow_dispatch)
    check_mirror "${CONFIG_SHA:?}" || status=1
    # betterleaks' own arguments for a full history, with --text added.
    scan event "$CONFIG_SHA" --log-opts="--text --full-history --all --diff-filter=tuxdb" || status=1
    ;;
  *)
    error "The secrets scan does not support the $EVENT_NAME event."
    exit 1
    ;;
esac
exit "$status"
