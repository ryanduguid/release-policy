#!/usr/bin/env bash
# Release-readiness gates. Source this file; each gate returns 0 on pass,
# non-zero with a stderr message on fail. Fail closed (ADR-0001).
set -euo pipefail

PYTHON="${PYTHON:-python3}"
GH="${GH:-gh}"

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

gate_main_matches() {
  local expected="$1" repo="$2" main_sha
  main_sha="$("$GH" api -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$repo/git/ref/heads/main" --jq '.object.sha')"
  if [ "$main_sha" != "$expected" ]; then
    echo "gate_main_matches: origin main $main_sha != $expected" >&2
    return 1
  fi
}

gate_no_existing_release() {
  # Precondition: TAG must already have passed gate_tag_format. TAG is
  # interpolated into the jq program below; the format gate is what makes
  # that safe.
  local tag="$1" repo="$2" ids
  ids="$("$GH" api --paginate -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$repo/releases?per_page=100" \
    --jq ".[] | select(.tag_name == \"$tag\") | .id")"
  if [ -n "$ids" ]; then
    echo "gate_no_existing_release: a release for $tag already exists; refusing to replace it" >&2
    return 1
  fi
}

derive_name_version() {
  local version_command="${1:-}" name stem version
  name="$("$PYTHON" -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["name"])')"
  stem="$("$PYTHON" -c 'import re,sys; print(re.sub(r"[^A-Za-z0-9.]+","_",sys.argv[1]))' "$name")"
  version="$("$PYTHON" -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"].get("version",""))')"
  if [ -z "$version" ]; then
    if [ -z "$version_command" ]; then
      echo "derive_name_version: version is dynamic and no version-command was provided" >&2
      return 1
    fi
    version="$(bash -c "$version_command")"
  fi
  printf '%s\n%s\n' "$stem" "$version"
}

derive_archive_metadata() {
  local artifact_stem="$1" version_file="$2"
  "$PYTHON" - "$artifact_stem" "$version_file" <<'PY' | tr -d '\r'
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import sys


stem, supplied_path = sys.argv[1:]
if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", stem) is None:
    raise SystemExit("derive_archive_metadata: artifact-stem must be lower-case and hyphenated")

if "\\" in supplied_path:
    raise SystemExit("derive_archive_metadata: version-file must use a safe relative POSIX path")
relative = PurePosixPath(supplied_path)
if (
    not supplied_path
    or relative.is_absolute()
    or any(part in {"", ".", ".."} for part in relative.parts)
):
    raise SystemExit("derive_archive_metadata: version-file must be a safe relative path")

root = Path.cwd().resolve()
candidate = (root / Path(*relative.parts)).resolve()
if candidate == root or root not in candidate.parents or not candidate.is_file():
    raise SystemExit("derive_archive_metadata: version-file is missing, outside the checkout, or not a file")

try:
    text = candidate.read_bytes().decode("utf-8")
except UnicodeDecodeError as error:
    raise SystemExit("derive_archive_metadata: version-file must be UTF-8") from error
if text.startswith("\ufeff"):
    raise SystemExit("derive_archive_metadata: version-file must not contain a BOM")
match = re.fullmatch(
    r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))(?:\r?\n)?",
    text,
)
if match is None:
    raise SystemExit("derive_archive_metadata: version-file must contain one canonical MAJOR.MINOR.PATCH line")

print(stem)
print(match.group(1))
PY
}

run_release_gates() {
  local tag="$1" head_sha="$2" repo="$3" version_command="${4:-}" stem version
  gate_tag_format "$tag"
  gate_annotated_tag "$tag"
  gate_tag_commit_matches "$tag" "$head_sha"
  gate_main_matches "$head_sha" "$repo"
  gate_notes_header "$tag"
  gate_clean_tree
  gate_no_existing_release "$tag" "$repo"
  { read -r stem; read -r version; } < <(derive_name_version "$version_command")
  if [ "$tag" != "v$version" ]; then
    echo "run_release_gates: tag $tag != v$version from project metadata" >&2
    return 1
  fi
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "tag=$tag"
      echo "version=$version"
      echo "stem=$stem"
      echo "commit=$head_sha"
    } >> "$GITHUB_OUTPUT"
  fi
}

run_archive_release_gates() {
  local tag="$1" head_sha="$2" repo="$3" artifact_stem="$4" version_file="$5"
  local stem version
  gate_tag_format "$tag"
  gate_annotated_tag "$tag"
  gate_tag_commit_matches "$tag" "$head_sha"
  gate_main_matches "$head_sha" "$repo"
  gate_notes_header "$tag"
  gate_clean_tree
  gate_no_existing_release "$tag" "$repo"
  { read -r stem; read -r version; } < <(derive_archive_metadata "$artifact_stem" "$version_file")
  if [ "$tag" != "v$version" ]; then
    echo "run_archive_release_gates: tag $tag != v$version from $version_file" >&2
    return 1
  fi
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "tag=$tag"
      echo "version=$version"
      echo "stem=$stem"
      echo "prefix=$stem-$version/"
      echo "commit=$head_sha"
    } >> "$GITHUB_OUTPUT"
  fi
}
