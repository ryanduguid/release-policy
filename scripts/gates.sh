#!/usr/bin/env bash
# Release-readiness gates. Source this file; each gate returns 0 on pass,
# non-zero with a stderr message on fail. Fail closed (ADR-0001).
set -euo pipefail

PYTHON="${PYTHON:-python3}"

gate_tag_format() {
  local tag="$1"
  if [[ ! "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    echo "gate_tag_format: '$tag' is not canonical vMAJOR.MINOR.PATCH" >&2
    return 1
  fi
}

gate_annotated_tag() {
  local tag="$1"
  if [ "$(git cat-file -t "refs/tags/$tag" 2>/dev/null)" != "tag" ]; then
    echo "gate_annotated_tag: '$tag' is not an annotated tag object" >&2
    return 1
  fi
}

gate_tag_commit_matches() {
  local tag="$1" expected="$2" actual
  actual="$(git rev-parse "refs/tags/$tag^{commit}")"
  if [ "$actual" != "$expected" ]; then
    echo "gate_tag_commit_matches: tag commit $actual != expected $expected" >&2
    return 1
  fi
}

gate_notes_header() {
  local tag="$1" first
  if [ ! -f RELEASE_NOTES.md ]; then
    echo "gate_notes_header: RELEASE_NOTES.md missing" >&2
    return 1
  fi
  first="$(sed -n '1p' RELEASE_NOTES.md)"
  if [ "$first" != "# $tag" ]; then
    echo "gate_notes_header: first line '$first' != '# $tag'" >&2
    return 1
  fi
}

gate_clean_tree() {
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "gate_clean_tree: working tree has tracked modifications" >&2
    return 1
  fi
}
