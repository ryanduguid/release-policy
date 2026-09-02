#!/usr/bin/env bash
# Release-readiness gates. Source this file; each gate returns 0 on pass,
# non-zero with a stderr message on fail. Fail closed (ADR-0001).
set -euo pipefail

PYTHON="${PYTHON:-python3}"
GH="${GH:-gh}"
GATES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

gate_tag_format() {
  local tag="$1" tag_prefix="${2:-}"
  if ! derive_release_tag "$tag" "$tag_prefix" >/dev/null; then
    echo "gate_tag_format: '$tag' is not the canonical tag for prefix '$tag_prefix'" >&2
    return 1
  fi
}

validate_release_inputs() {
  local source_directory="$1" tag_prefix="$2"
  "$PYTHON" "$GATES_DIR/python_release.py" validate-inputs \
    --root . \
    --source-directory "$source_directory" \
    --tag-prefix "$tag_prefix" \
    --format lines \
    | tr -d '\r'
}

write_release_input_outputs() {
  local supplied_source="$1" supplied_prefix="$2" checkout_directory="$3"
  local source_directory tag_prefix source_root source_path
  { read -r source_directory; read -r tag_prefix; } < <(
    validate_release_inputs "$supplied_source" "$supplied_prefix"
  )
  source_root="$checkout_directory"
  if [ "$source_directory" != "." ]; then
    source_root="$source_root/$source_directory"
  fi
  source_path="$GITHUB_WORKSPACE/$source_root"
  {
    echo "source-directory=$source_directory"
    echo "tag-prefix=$tag_prefix"
    echo "source-root=$source_root"
    echo "source-path=$source_path"
  } >> "$GITHUB_OUTPUT"
}

derive_release_tag() {
  local tag="$1" tag_prefix="$2"
  "$PYTHON" "$GATES_DIR/python_release.py" tag \
    --tag "$tag" \
    --tag-prefix "$tag_prefix" \
    --format lines \
    | tr -d '\r'
}

gate_component_identity() {
  local source_directory="$1" tag_prefix="$2" release_identity="$3" identity_label="$4"
  local directory_identity
  if [ "$source_directory" = "." ]; then
    if [ -n "$tag_prefix" ]; then
      echo "gate_component_identity: root releases require an empty tag-prefix" >&2
      return 1
    fi
    return 0
  fi

  directory_identity="${source_directory##*/}"
  if [ -z "$tag_prefix" ]; then
    echo "gate_component_identity: nested releases require a tag-prefix" >&2
    return 1
  fi
  if [ "$directory_identity" != "$tag_prefix" ]; then
    echo "gate_component_identity: directory identity '$directory_identity' != tag-prefix '$tag_prefix'" >&2
    return 1
  fi
  if [ "$release_identity" != "$tag_prefix" ]; then
    echo "gate_component_identity: $identity_label '$release_identity' != tag-prefix '$tag_prefix'" >&2
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
  local version_tag="$1" source_directory="${2:-.}" first notes
  notes="$source_directory/RELEASE_NOTES.md"
  if [ ! -f "$notes" ]; then
    echo "gate_notes_header: RELEASE_NOTES.md missing" >&2
    return 1
  fi
  first="$(sed -n '1p' "$notes")"
  if [ "$first" != "# $version_tag" ]; then
    echo "gate_notes_header: first line '$first' != '# $version_tag'" >&2
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
  local version_parser="$1" version_file="$2" source_directory="${3:-.}"
  "$PYTHON" "$GATES_DIR/python_release.py" metadata \
    --root "$source_directory" \
    --version-parser "$version_parser" \
    --version-file "$version_file" \
    --format lines \
    | tr -d '\r'
}

derive_archive_metadata() {
  local artifact_stem="$1" version_file="$2" supplied_source="${3:-.}"
  local source_directory _
  { read -r source_directory; read -r _; } < <(
    validate_release_inputs "$supplied_source" ""
  )
  "$PYTHON" - "$artifact_stem" "$version_file" "$source_directory" <<'PY' | tr -d '\r'
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import sys


stem, supplied_path, source_directory = sys.argv[1:]
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

root = (Path.cwd() / source_directory).resolve()
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
  local tag="$1" head_sha="$2" repo="$3" version_parser="$4" version_file="$5"
  local supplied_source="${6:-.}" supplied_prefix="${7:-}"
  local source_directory tag_prefix stem version tag_version version_tag full_tag artifact_tag
  local distribution_identity
  { read -r source_directory; read -r tag_prefix; } < <(
    validate_release_inputs "$supplied_source" "$supplied_prefix"
  )
  gate_tag_format "$tag" "$tag_prefix"
  { read -r tag_version; read -r version_tag; read -r full_tag; read -r artifact_tag; } < <(
    derive_release_tag "$tag" "$tag_prefix"
  )
  gate_annotated_tag "$tag"
  gate_tag_commit_matches "$tag" "$head_sha"
  gate_main_matches "$head_sha" "$repo"
  gate_notes_header "$version_tag" "$source_directory"
  gate_clean_tree
  gate_no_existing_release "$tag" "$repo"
  { read -r stem; read -r version; } < <(
    derive_name_version "$version_parser" "$version_file" "$source_directory"
  )
  distribution_identity="${stem//_/-}"
  if ! gate_component_identity \
    "$source_directory" "$tag_prefix" "$distribution_identity" "distribution identity"; then
    return 1
  fi
  if [ "$tag_version" != "$version" ]; then
    echo "run_release_gates: tag version $tag_version != $version from project metadata" >&2
    return 1
  fi
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "tag=$full_tag"
      echo "version-tag=$version_tag"
      echo "artifact-tag=$artifact_tag"
      echo "source-directory=$source_directory"
      echo "tag-prefix=$tag_prefix"
      echo "version=$version"
      echo "stem=$stem"
      echo "commit=$head_sha"
    } >> "$GITHUB_OUTPUT"
  fi
}

run_archive_release_gates() {
  local tag="$1" head_sha="$2" repo="$3" artifact_stem="$4" version_file="$5"
  local supplied_source="${6:-.}" supplied_prefix="${7:-}"
  local source_directory tag_prefix stem version tag_version version_tag full_tag artifact_tag
  { read -r source_directory; read -r tag_prefix; } < <(
    validate_release_inputs "$supplied_source" "$supplied_prefix"
  )
  gate_tag_format "$tag" "$tag_prefix"
  { read -r tag_version; read -r version_tag; read -r full_tag; read -r artifact_tag; } < <(
    derive_release_tag "$tag" "$tag_prefix"
  )
  gate_annotated_tag "$tag"
  gate_tag_commit_matches "$tag" "$head_sha"
  gate_main_matches "$head_sha" "$repo"
  gate_notes_header "$version_tag" "$source_directory"
  gate_clean_tree
  gate_no_existing_release "$tag" "$repo"
  { read -r stem; read -r version; } < <(
    derive_archive_metadata "$artifact_stem" "$version_file" "$source_directory"
  )
  if ! gate_component_identity \
    "$source_directory" "$tag_prefix" "$stem" "artifact-stem"; then
    return 1
  fi
  if [ "$tag_version" != "$version" ]; then
    echo "run_archive_release_gates: tag version $tag_version != $version from $version_file" >&2
    return 1
  fi
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "tag=$full_tag"
      echo "version-tag=$version_tag"
      echo "artifact-tag=$artifact_tag"
      echo "source-directory=$source_directory"
      echo "tag-prefix=$tag_prefix"
      echo "version=$version"
      echo "stem=$stem"
      echo "prefix=$stem-$version/"
      echo "commit=$head_sha"
    } >> "$GITHUB_OUTPUT"
  fi
}
