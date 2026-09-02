#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATES="$HERE/../scripts/gates.sh"
FAILURES=0
command -v python3 >/dev/null 2>&1 || export PYTHON=python

run_case() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "ok   $name"; else echo "FAIL $name"; FAILURES=$((FAILURES+1)); fi
}

expect_fail() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "FAIL $name (expected failure)"; FAILURES=$((FAILURES+1)); else echo "ok   $name"; fi
}

make_repo() {
  local dir
  dir="$(mktemp -d)"
  cd "$dir"
  git init --quiet -b main
  git config user.name "Test" && git config user.email "test@example.invalid"
  git config tag.gpgSign false
  printf '# v1.2.3\n\nNotes.\n' > RELEASE_NOTES.md
  git add -A && git commit --quiet -m "init"
}

finish() {
  echo "----"
  if [ "$FAILURES" -eq 0 ]; then echo "all gate tests passed"; else echo "$FAILURES gate test(s) failed"; exit 1; fi
}

# shellcheck source=scripts/gates.sh
. "$GATES"

# --- tag format ---
run_case  "tag format accepts v1.2.3"        gate_tag_format "v1.2.3"
run_case  "tag format accepts v0.1.0"        gate_tag_format "v0.1.0"
run_case  "tag format accepts exact namespace" \
  gate_tag_format "payday-super-checker/v1.2.3" "payday-super-checker"
expect_fail "tag format rejects v1.2"        gate_tag_format "v1.2"
expect_fail "tag format rejects v01.2.3"     gate_tag_format "v01.2.3"
expect_fail "tag format rejects 1.2.3"       gate_tag_format "1.2.3"
expect_fail "tag format rejects v1.2.3-rc1"  gate_tag_format "v1.2.3-rc1"
expect_fail "tag format rejects namespace mismatch" \
  gate_tag_format "other/v1.2.3" "payday-super-checker"

# --- component-directory and tag-prefix validation ---
make_repo
mkdir -p packages/example-tool
printf 'tracked\n' > packages/example-tool/source.txt
mkdir -p 'packages/[glob]'
printf 'tracked\n' > 'packages/[glob]/source.txt'
git add packages/example-tool/source.txt 'packages/[glob]/source.txt' \
  && git commit --quiet -m "tracked components"
run_case "release inputs accept root defaults" validate_release_inputs "." ""
run_case "release inputs accept tracked nested component" \
  validate_release_inputs "packages/example-tool" ""
run_case "release inputs accept lower-case hyphenated prefix" \
  validate_release_inputs "packages/example-tool" "example-tool"
root_input_outputs="$(mktemp)"
GITHUB_OUTPUT="$root_input_outputs" GITHUB_WORKSPACE="$PWD/workspace" \
  run_case "release input outputs preserve the root checkout path" \
    write_release_input_outputs "." "" source
run_case "root input outputs contain the root source path" \
  grep -q "^source-path=$PWD/workspace/source$" "$root_input_outputs"
nested_input_outputs="$(mktemp)"
GITHUB_OUTPUT="$nested_input_outputs" GITHUB_WORKSPACE="$PWD/workspace" \
  run_case "release input outputs scope the nested checkout path" \
    write_release_input_outputs "packages/example-tool" "example-tool" source
run_case "nested input outputs contain the component source path" \
  grep -q "^source-path=$PWD/workspace/source/packages/example-tool$" \
    "$nested_input_outputs"
run_case "nested input outputs preserve the validated tag prefix" \
  grep -q '^tag-prefix=example-tool$' "$nested_input_outputs"
expect_fail "release inputs reject empty source directory" validate_release_inputs "" ""
expect_fail "release inputs reject absolute source directory" \
  validate_release_inputs "$PWD/packages/example-tool" ""
expect_fail "release inputs reject source traversal" \
  validate_release_inputs "../example-tool" ""
expect_fail "release inputs reject backslash source path" \
  validate_release_inputs 'packages\example-tool' ""
expect_fail "release inputs reject artifact-glob source path" \
  validate_release_inputs 'packages/[glob]' ""
expect_fail "release inputs reject tracked file as source directory" \
  validate_release_inputs "RELEASE_NOTES.md" ""
mkdir -p packages/untracked
printf 'untracked\n' > packages/untracked/source.txt
expect_fail "release inputs reject untracked source directory" \
  validate_release_inputs "packages/untracked" ""
if ln -s example-tool packages/linked-tool 2>/dev/null; then
  expect_fail "release inputs reject symlinked source directory" \
    validate_release_inputs "packages/linked-tool" ""
else
  echo "skip release input symlink case (symlinks unavailable)"
fi
for prefix in "Example-Tool" "example/tool" "example tool" "example--tool" \
  "-example" "example-" 'example;touch executed'; do
  expect_fail "release inputs reject prefix: $prefix" \
    validate_release_inputs "packages/example-tool" "$prefix"
done
run_case "prefix metacharacter fixture did not execute" test ! -e executed

# --- annotated tag + commit match ---
make_repo
git tag -a v1.2.3 -m "release v1.2.3"
head_sha="$(git rev-parse HEAD)"
run_case  "annotated tag accepted"           gate_annotated_tag "v1.2.3"
run_case  "tag commit matches HEAD"          gate_tag_commit_matches "v1.2.3" "$head_sha"
expect_fail "wrong expected sha rejected"    gate_tag_commit_matches "v1.2.3" "0000000000000000000000000000000000000000"

make_repo
git tag v9.9.9   # lightweight
expect_fail "lightweight tag rejected"       gate_annotated_tag "v9.9.9"

# --- notes header ---
make_repo
run_case  "notes header matches tag"     gate_notes_header "v1.2.3"
expect_fail "notes header mismatch"      gate_notes_header "v1.2.4"
rm RELEASE_NOTES.md
expect_fail "missing notes file"         gate_notes_header "v1.2.3"

# --- clean tree ---
make_repo
run_case  "clean tree accepted"          gate_clean_tree
echo dirty >> RELEASE_NOTES.md
expect_fail "dirty tree rejected"        gate_clean_tree

# --- API gates via stub gh ---
make_repo
stub_dir="$(mktemp -d)"
cat > "$stub_dir/gh" <<'STUB'
#!/usr/bin/env bash
# Stub: emits canned responses driven by env vars.
case "$*" in
  *git/ref/heads/main*) printf '%s\n' "${STUB_MAIN_SHA:?}" ;;
  *releases*)           printf '%s'   "${STUB_RELEASE_IDS:-}" ;;
  *) echo "stub gh: unexpected args: $*" >&2; exit 64 ;;
esac
STUB
chmod +x "$stub_dir/gh"
export GH="$stub_dir/gh"

export STUB_MAIN_SHA="abc123"
run_case  "main matches"                  gate_main_matches "abc123" "ryanduguid/example"
expect_fail "main mismatch"               gate_main_matches "def456" "ryanduguid/example"

export STUB_RELEASE_IDS=""
run_case  "no existing release"           gate_no_existing_release "v1.2.3" "ryanduguid/example"
export STUB_RELEASE_IDS="12345"
expect_fail "existing release rejected"   gate_no_existing_release "v1.2.3" "ryanduguid/example"
unset GH STUB_MAIN_SHA STUB_RELEASE_IDS

# --- name/version derivation ---
make_repo
cat > pyproject.toml <<'EOF'
[project]
name = "demo-pkg"
version = "1.2.3"
EOF
git add pyproject.toml && git commit --quiet -m "static metadata"
run_case "derive static version" \
  test "$(derive_name_version "pyproject" "pyproject.toml" | tr '\n' ' ')" = "demo_pkg 1.2.3 "

cat > pyproject.toml <<'EOF'
[project]
name = "dyn-pkg"
dynamic = ["version"]
EOF
cat > version.py <<'EOF'
raise RuntimeError("the release gate must not import this file")
__version__ = "9.8.7"
EOF
git add pyproject.toml version.py && git commit --quiet -m "dynamic metadata"
run_case "derive dynamic version via literal parser" \
  test "$(derive_name_version "python-literal" "version.py" | tr '\n' ' ')" = "dyn_pkg 9.8.7 "
expect_fail "unknown parser fails closed" derive_name_version "shell" "version.py"
expect_fail "untracked version file fails closed" derive_name_version "python-literal" "missing.py"
expect_fail "shell metacharacters are data, never commands" \
  derive_name_version "python-literal" "version.py; touch executed"
run_case "metacharacter fixture did not execute" test ! -e executed

mkdir -p packages/nested-pkg
cat > packages/nested-pkg/pyproject.toml <<'EOF'
[project]
name = "nested-pkg"
version = "1.2.3"
EOF
git add packages/nested-pkg/pyproject.toml && git commit --quiet -m "nested metadata"
run_case "derive nested static version" \
  test "$(derive_name_version "pyproject" "pyproject.toml" "packages/nested-pkg" | tr '\n' ' ')" = "nested_pkg 1.2.3 "

# --- entrypoint ---
make_repo
cat > pyproject.toml <<'EOF'
[project]
name = "demo-pkg"
version = "1.2.3"
EOF
git add -A && git commit --quiet -m "pyproject"
git tag -a v1.2.3 -m "release v1.2.3"
git tag -a root-prefix/v1.2.3 -m "release root-prefix v1.2.3"
head_sha="$(git rev-parse HEAD)"
export GH="$stub_dir/gh" STUB_MAIN_SHA="$head_sha" STUB_RELEASE_IDS=""
out_file="$(mktemp)"
GITHUB_OUTPUT="$out_file" run_case "entrypoint passes and writes outputs" \
  run_release_gates "v1.2.3" "$head_sha" "ryanduguid/example" "pyproject" "pyproject.toml"
run_case "outputs contain stem" grep -q '^stem=demo_pkg$' "$out_file"
run_case "outputs contain version" grep -q '^version=1.2.3$' "$out_file"
expect_fail "Python root release rejects a non-empty tag prefix" \
  run_release_gates "root-prefix/v1.2.3" "$head_sha" \
    "ryanduguid/example" "pyproject" "pyproject.toml" "." "root-prefix"

git tag -a v1.2.4 -m "mismatched"   # tag does not match pyproject version
expect_fail "version/tag mismatch rejected" \
  run_release_gates "v1.2.4" "$head_sha" "ryanduguid/example" "pyproject" "pyproject.toml"
unset GH STUB_MAIN_SHA STUB_RELEASE_IDS

# --- namespaced Python entrypoint ---
make_repo
mkdir -p packages/payday-super-checker
cat > packages/payday-super-checker/pyproject.toml <<'EOF'
[project]
name = "payday-super-checker"
version = "1.2.3"
EOF
printf '# v1.2.3\n\nComponent notes.\n' > packages/payday-super-checker/RELEASE_NOTES.md
mkdir -p packages/target-prefix
cat > packages/target-prefix/pyproject.toml <<'EOF'
[project]
name = "sibling-package"
version = "1.2.3"
EOF
printf '# v1.2.3\n\nComponent notes.\n' > packages/target-prefix/RELEASE_NOTES.md
mkdir -p packages/demo-pkg
cat > packages/demo-pkg/pyproject.toml <<'EOF'
[project]
name = "Demo.Pkg"
version = "1.2.3"
EOF
printf '# v1.2.3\n\nComponent notes.\n' > packages/demo-pkg/RELEASE_NOTES.md
git add -A && git commit --quiet -m "nested package"
git tag -a payday-super-checker/v1.2.3 -m "release payday-super-checker v1.2.3"
git tag -a v1.2.3 -m "release unprefixed v1.2.3"
git tag -a target-prefix/v1.2.3 -m "release target-prefix v1.2.3"
git tag -a demo-pkg/v1.2.3 -m "release demo-pkg v1.2.3"
head_sha="$(git rev-parse HEAD)"
export GH="$stub_dir/gh" STUB_MAIN_SHA="$head_sha" STUB_RELEASE_IDS=""
namespaced_out="$(mktemp)"
GITHUB_OUTPUT="$namespaced_out" run_case "namespaced entrypoint passes" \
  run_release_gates "payday-super-checker/v1.2.3" "$head_sha" \
    "ryanduguid/example" "pyproject" "pyproject.toml" \
    "packages/payday-super-checker" "payday-super-checker"
run_case "namespaced outputs contain full tag" \
  grep -q '^tag=payday-super-checker/v1.2.3$' "$namespaced_out"
run_case "namespaced outputs contain plain version tag" \
  grep -q '^version-tag=v1.2.3$' "$namespaced_out"
run_case "namespaced outputs contain slash-free artifact tag" \
  grep -q '^artifact-tag=payday-super-checker-v1.2.3$' "$namespaced_out"
run_case "namespaced outputs contain component directory" \
  grep -q '^source-directory=packages/payday-super-checker$' "$namespaced_out"
expect_fail "nested Python release rejects an empty tag prefix" \
  run_release_gates "v1.2.3" "$head_sha" \
    "ryanduguid/example" "pyproject" "pyproject.toml" \
    "packages/payday-super-checker" ""
expect_fail "Python release rejects a directory leaf and prefix mismatch" \
  run_release_gates "target-prefix/v1.2.3" "$head_sha" \
    "ryanduguid/example" "pyproject" "pyproject.toml" \
    "packages/payday-super-checker" "target-prefix"
expect_fail "Python release rejects a distribution and prefix mismatch" \
  run_release_gates "target-prefix/v1.2.3" "$head_sha" \
    "ryanduguid/example" "pyproject" "pyproject.toml" \
    "packages/target-prefix" "target-prefix"
run_case "Python release accepts dotted distribution normalised to its prefix" \
  run_release_gates "demo-pkg/v1.2.3" "$head_sha" \
    "ryanduguid/example" "pyproject" "pyproject.toml" \
    "packages/demo-pkg" "demo-pkg"
unset GH STUB_MAIN_SHA STUB_RELEASE_IDS

# --- source-archive metadata ---
make_repo
printf '1.2.3\n' > VERSION
git add VERSION && git commit --quiet -m "archive version"
run_case "derive archive metadata" \
  test "$(derive_archive_metadata "example-toolkit" "VERSION" | tr '\n' ' ')" = "example-toolkit 1.2.3 "
expect_fail "archive stem rejects uppercase" derive_archive_metadata "Example-Toolkit" "VERSION"
expect_fail "archive stem rejects path separators" derive_archive_metadata "../example" "VERSION"
expect_fail "archive version path rejects traversal" derive_archive_metadata "example-toolkit" "../VERSION"
expect_fail "archive version path rejects a directory" derive_archive_metadata "example-toolkit" "."

printf '01.2.3\n' > VERSION
expect_fail "archive version rejects leading zero" derive_archive_metadata "example-toolkit" "VERSION"
printf '1.2.3\nextra\n' > VERSION
expect_fail "archive version rejects multiple lines" derive_archive_metadata "example-toolkit" "VERSION"
printf '1.2.3\n' > VERSION
git tag -a v1.2.3 -m "release v1.2.3"
git tag -a root-archive/v1.2.3 -m "release root-archive v1.2.3"
head_sha="$(git rev-parse HEAD)"
export GH="$stub_dir/gh" STUB_MAIN_SHA="$head_sha" STUB_RELEASE_IDS=""
archive_out="$(mktemp)"
GITHUB_OUTPUT="$archive_out" run_case "archive entrypoint passes and writes outputs" \
  run_archive_release_gates "v1.2.3" "$head_sha" "ryanduguid/example" "example-toolkit" "VERSION"
run_case "archive outputs contain stem" grep -q '^stem=example-toolkit$' "$archive_out"
run_case "archive outputs contain version" grep -q '^version=1.2.3$' "$archive_out"
run_case "archive outputs contain prefix" grep -q '^prefix=example-toolkit-1.2.3/$' "$archive_out"
expect_fail "archive root release rejects a non-empty tag prefix" \
  run_archive_release_gates "root-archive/v1.2.3" "$head_sha" \
    "ryanduguid/example" "example-toolkit" "VERSION" "." "root-archive"
expect_fail "archive tag/version mismatch rejected" \
  run_archive_release_gates "v1.2.4" "$head_sha" "ryanduguid/example" "example-toolkit" "VERSION"
unset GH STUB_MAIN_SHA STUB_RELEASE_IDS

# --- namespaced source-archive entrypoint ---
make_repo
mkdir -p packages/example-toolkit
printf '1.2.3\n' > packages/example-toolkit/VERSION
printf '# v1.2.3\n\nComponent notes.\n' > packages/example-toolkit/RELEASE_NOTES.md
mkdir -p packages/target-prefix
printf '1.2.3\n' > packages/target-prefix/VERSION
printf '# v1.2.3\n\nComponent notes.\n' > packages/target-prefix/RELEASE_NOTES.md
git add -A && git commit --quiet -m "nested archive"
run_case "derive nested archive metadata" \
  test "$(derive_archive_metadata "example-toolkit" "VERSION" "packages/example-toolkit" | tr '\n' ' ')" = "example-toolkit 1.2.3 "
git tag -a example-toolkit/v1.2.3 -m "release example-toolkit v1.2.3"
git tag -a v1.2.3 -m "release unprefixed v1.2.3"
git tag -a target-prefix/v1.2.3 -m "release target-prefix v1.2.3"
head_sha="$(git rev-parse HEAD)"
export GH="$stub_dir/gh" STUB_MAIN_SHA="$head_sha" STUB_RELEASE_IDS=""
namespaced_archive_out="$(mktemp)"
GITHUB_OUTPUT="$namespaced_archive_out" run_case "namespaced archive entrypoint passes" \
  run_archive_release_gates "example-toolkit/v1.2.3" "$head_sha" \
    "ryanduguid/example" "example-toolkit" "VERSION" \
    "packages/example-toolkit" "example-toolkit"
run_case "namespaced archive outputs contain full tag" \
  grep -q '^tag=example-toolkit/v1.2.3$' "$namespaced_archive_out"
run_case "namespaced archive outputs contain plain version tag" \
  grep -q '^version-tag=v1.2.3$' "$namespaced_archive_out"
run_case "namespaced archive outputs contain slash-free artifact tag" \
  grep -q '^artifact-tag=example-toolkit-v1.2.3$' "$namespaced_archive_out"
expect_fail "nested archive release rejects an empty tag prefix" \
  run_archive_release_gates "v1.2.3" "$head_sha" \
    "ryanduguid/example" "example-toolkit" "VERSION" \
    "packages/example-toolkit" ""
expect_fail "archive release rejects a directory leaf and prefix mismatch" \
  run_archive_release_gates "target-prefix/v1.2.3" "$head_sha" \
    "ryanduguid/example" "target-prefix" "VERSION" \
    "packages/example-toolkit" "target-prefix"
expect_fail "archive release rejects an artifact stem and prefix mismatch" \
  run_archive_release_gates "target-prefix/v1.2.3" "$head_sha" \
    "ryanduguid/example" "sibling-archive" "VERSION" \
    "packages/target-prefix" "target-prefix"
unset GH STUB_MAIN_SHA STUB_RELEASE_IDS

finish
