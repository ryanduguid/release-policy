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
  printf '# v1.2.3\n\nNotes.\n' > RELEASE_NOTES.md
  git add -A && git commit --quiet -m "init"
}

finish() {
  echo "----"
  if [ "$FAILURES" -eq 0 ]; then echo "all gate tests passed"; else echo "$FAILURES gate test(s) failed"; exit 1; fi
}

# shellcheck source=../scripts/gates.sh
. "$GATES"

# --- tag format ---
run_case  "tag format accepts v1.2.3"        gate_tag_format "v1.2.3"
run_case  "tag format accepts v0.1.0"        gate_tag_format "v0.1.0"
expect_fail "tag format rejects v1.2"        gate_tag_format "v1.2"
expect_fail "tag format rejects v01.2.3"     gate_tag_format "v01.2.3"
expect_fail "tag format rejects 1.2.3"       gate_tag_format "1.2.3"
expect_fail "tag format rejects v1.2.3-rc1"  gate_tag_format "v1.2.3-rc1"

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

finish
