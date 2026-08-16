# Release-Policy Module (Phase 0 + Pilot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `ryanduguid/release-policy` (reusable release workflow + testable gates) and migrate the three packaged-Python repos to it, closing their superseded hardening drafts.

**Architecture:** A dedicated repository owns the release policy as a `workflow_call` reusable workflow whose gates live in a bash script (`scripts/gates.sh`) testable against synthetic git repositories. Consumers keep a ~15-line caller pinned to a full commit SHA. Read-only checks and publish permissions stay separated; every gate fails closed.

**Tech Stack:** GitHub Actions (reusable workflows), bash, Python 3.12 + tomllib, `uv==0.12.0` in consumers, `python -m build`, anchore/sbom-action, actions/attest, `gh` CLI.

## Global Constraints

- Every third-party action pinned to a full 40-char commit SHA with a trailing version comment. Known-good pins already in use across the account, reuse verbatim:
  - `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`
  - `actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0`
  - `anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610 # v0.24.0` (with `syft-version: v1.51.0`)
  - `actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4.2.2`
- `persist-credentials: false` on every checkout; `set -euo pipefail` in every multi-line shell step.
- The module never creates tags, never overwrites an existing release, never retries into a publish (ADR-0001).
- Git author for every commit: `Ryan Duguid <ryan@duguid.com.au>`. Never the pm.me address.
- Licence: MIT (matches all three pilot repos).
- Prose style: no em dashes in any newly written markdown.
- Local test environment is Git Bash on Windows: scripts must work with `python` or `python3` (use `PYTHON="${PYTHON:-python3}"`; the test harness sets `PYTHON=python` when `python3` is absent).
- Repo working directory: `C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad/release-policy` (existing local git repo, branch `main`, ADR + spec already committed).
- ---

### Task 1: Repository scaffold

**Files:**
- Create: `LICENSE`
- Create: `README.md` (stub; full consumer docs in Task 8)
- Create: `RELEASE_NOTES.md`

**Interfaces:**
- Produces: repo root files later tasks assume present. `RELEASE_NOTES.md` first line must be `# v0.1.0`.

- [ ] **Step 1: Write LICENSE**

MIT licence text, copyright line: `Copyright (c) 2026 Ryan Duguid`. Copy the exact MIT text from any of the three pilot repos to stay word-identical with the account norm:

```bash
cd "C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad/release-policy"
gh api repos/ryanduguid/monthly-close-control-plane/contents/LICENSE --jq '.content' | base64 -d > LICENSE
```

- [ ] **Step 2: Write README.md stub**

```markdown
# release-policy

Shared release policy for ryanduguid repositories: reusable GitHub Actions
workflows plus testable release-readiness gates. Governed by
[ADR-0001](./docs/adr/0001-shared-release-policy.md); design in
[docs/superpowers/specs/](./docs/superpowers/specs/).

Consumer documentation lands with the first module release.
```

- [ ] **Step 3: Write RELEASE_NOTES.md**

```markdown
# v0.1.0

Initial module: release-readiness gates (`scripts/gates.sh`), the
packaged-Python reusable release workflow, module CI, and consumer
documentation. Governed by ADR-0001.
```

- [ ] **Step 4: Commit**

```bash
git add LICENSE README.md RELEASE_NOTES.md
git commit -m "Scaffold repository: MIT licence, README stub, release notes"
```

---

### Task 2: Gate test harness + local git gates

**Files:**
- Create: `tests/test_gates.sh`
- Create: `scripts/gates.sh`

**Interfaces:**
- Produces: `scripts/gates.sh` sourceable bash defining (this task): `gate_tag_format TAG`, `gate_annotated_tag TAG`, `gate_tag_commit_matches TAG EXPECTED_SHA`. Each returns 0 on pass, non-zero with a stderr message on fail. Also `PYTHON` var default. `tests/test_gates.sh` exposes helpers reused by later tasks: `run_case NAME CMD...`, `expect_fail NAME CMD...`, `make_repo` (creates a synthetic repo in `$TMPDIR`, cd's into it, one commit on `main`, prints nothing), `finish` (exit summary).

- [ ] **Step 1: Write the failing test**

`tests/test_gates.sh`:

```bash
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

finish
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad/release-policy"
bash tests/test_gates.sh
```

Expected: FAIL, sourcing `scripts/gates.sh` errors because the file does not exist.

- [ ] **Step 3: Write minimal implementation**

`scripts/gates.sh`:

```bash
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
bash tests/test_gates.sh
```

Expected: `all gate tests passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/gates.sh tests/test_gates.sh
git commit -m "Add gate test harness and local git gates (tag format, annotated, commit match)"
```

---

### Task 3: Notes-header and clean-tree gates

**Files:**
- Modify: `scripts/gates.sh` (append two functions)
- Modify: `tests/test_gates.sh` (insert cases before `finish`)

**Interfaces:**
- Consumes: harness helpers from Task 2.
- Produces: `gate_notes_header TAG` (first line of `RELEASE_NOTES.md` equals `# TAG`), `gate_clean_tree` (no tracked modifications).

- [ ] **Step 1: Add failing tests** (insert into `tests/test_gates.sh` immediately before `finish`)

```bash
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
```

- [ ] **Step 2: Run to verify the new cases fail**

```bash
bash tests/test_gates.sh
```

Expected: the five new cases FAIL (functions undefined), earlier cases still pass.

- [ ] **Step 3: Implement** (append to `scripts/gates.sh`)

```bash
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
```

- [ ] **Step 4: Run full suite, expect all pass**

```bash
bash tests/test_gates.sh
```

- [ ] **Step 5: Commit**

```bash
git add scripts/gates.sh tests/test_gates.sh
git commit -m "Add notes-header and clean-tree gates"
```

---

### Task 4: API-backed gates with a stubbed gh

**Files:**
- Modify: `scripts/gates.sh`
- Modify: `tests/test_gates.sh`

**Interfaces:**
- Consumes: harness from Task 2.
- Produces: `gate_main_matches EXPECTED_SHA REPO` (GitHub API `heads/main` equals sha) and `gate_no_existing_release TAG REPO` (no release exists for tag, paginated). Both call the command named by `GH` (default `gh`), so tests inject a stub.

- [ ] **Step 1: Add failing tests** (insert before `finish`)

```bash
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
```

- [ ] **Step 2: Run to verify the new cases fail**

```bash
bash tests/test_gates.sh
```

- [ ] **Step 3: Implement** (append to `scripts/gates.sh`)

```bash
GH="${GH:-gh}"

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
  local tag="$1" repo="$2" ids
  ids="$("$GH" api --paginate -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$repo/releases?per_page=100" \
    --jq ".[] | select(.tag_name == \"$tag\") | .id")"
  if [ -n "$ids" ]; then
    echo "gate_no_existing_release: a release for $tag already exists; refusing to replace it" >&2
    return 1
  fi
}
```

Note: `GH="${GH:-gh}"` must be assigned once near the top of the file with `PYTHON`, not inside a function; move it there during this step.

- [ ] **Step 4: Run full suite, expect all pass**

```bash
bash tests/test_gates.sh
```

- [ ] **Step 5: Commit**

```bash
git add scripts/gates.sh tests/test_gates.sh
git commit -m "Add API-backed gates for main match and release non-existence, gh injectable"
```

---

### Task 5: Name, stem and version derivation

**Files:**
- Modify: `scripts/gates.sh`
- Modify: `tests/test_gates.sh`

**Interfaces:**
- Consumes: harness; `PYTHON` var.
- Produces: `derive_name_version [VERSION_COMMAND]`. Reads `pyproject.toml` in cwd. Prints two lines: wheel stem (PEP 427 escaping of the project name: runs of characters outside `[A-Za-z0-9.]` become one `_`), then version. Version comes from `[project].version`; if absent (dynamic) the caller-supplied `VERSION_COMMAND` runs via `bash -c` and its stdout is the version; if neither exists, fail.

- [ ] **Step 1: Add failing tests** (insert before `finish`)

```bash
# --- name/version derivation ---
make_repo
cat > pyproject.toml <<'EOF'
[project]
name = "demo-pkg"
version = "1.2.3"
EOF
run_case "derive static version" test "$(derive_name_version | tr '\n' ' ')" = "demo_pkg 1.2.3 "

cat > pyproject.toml <<'EOF'
[project]
name = "dyn-pkg"
dynamic = ["version"]
EOF
run_case "derive dynamic version via command" \
  test "$(derive_name_version "echo 9.8.7" | tr '\n' ' ')" = "dyn_pkg 9.8.7 "
expect_fail "dynamic without command fails" derive_name_version
```

- [ ] **Step 2: Run to verify the new cases fail**

```bash
bash tests/test_gates.sh
```

- [ ] **Step 3: Implement** (append to `scripts/gates.sh`)

```bash
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
```

- [ ] **Step 4: Run full suite, expect all pass**

```bash
bash tests/test_gates.sh
```

- [ ] **Step 5: Commit**

```bash
git add scripts/gates.sh tests/test_gates.sh
git commit -m "Derive wheel stem and version from pyproject with dynamic-version fallback"
```

---

### Task 6: Gate runner entrypoint

**Files:**
- Modify: `scripts/gates.sh`
- Modify: `tests/test_gates.sh`

**Interfaces:**
- Consumes: every gate and `derive_name_version`.
- Produces: `run_release_gates TAG HEAD_SHA REPO [VERSION_COMMAND]`. Runs, in order: tag format, annotated tag, tag commit matches HEAD_SHA, main matches, notes header, clean tree, no existing release, then `derive_name_version`. On success writes `tag=`, `version=`, `stem=`, `commit=` lines to the file named by `$GITHUB_OUTPUT` when that variable is set, and additionally checks `TAG == "v$version"`. This is the single function the workflow calls.

- [ ] **Step 1: Add failing tests** (insert before `finish`)

```bash
# --- entrypoint ---
make_repo
cat > pyproject.toml <<'EOF'
[project]
name = "demo-pkg"
version = "1.2.3"
EOF
git add -A && git commit --quiet -m "pyproject"
git tag -a v1.2.3 -m "release v1.2.3"
head_sha="$(git rev-parse HEAD)"
export GH="$stub_dir/gh" STUB_MAIN_SHA="$head_sha" STUB_RELEASE_IDS=""
out_file="$(mktemp)"
GITHUB_OUTPUT="$out_file" run_case "entrypoint passes and writes outputs" \
  run_release_gates "v1.2.3" "$head_sha" "ryanduguid/example"
run_case "outputs contain stem" grep -q '^stem=demo_pkg$' "$out_file"
run_case "outputs contain version" grep -q '^version=1.2.3$' "$out_file"

git tag -a v1.2.4 -m "mismatched"   # tag does not match pyproject version
expect_fail "version/tag mismatch rejected" \
  run_release_gates "v1.2.4" "$head_sha" "ryanduguid/example"
unset GH STUB_MAIN_SHA STUB_RELEASE_IDS
```

Note: the v1.2.4 tag points at the same commit as HEAD, `STUB_MAIN_SHA` still matches, and the notes-header gate must therefore be ordered before the notes check would pass; the failure this case asserts is the `TAG == v$version` check (notes header also mismatches, either failure is a correct fail-closed result).

- [ ] **Step 2: Run to verify the new cases fail**

```bash
bash tests/test_gates.sh
```

- [ ] **Step 3: Implement** (append to `scripts/gates.sh`)

```bash
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
```

- [ ] **Step 4: Run full suite, expect all pass**

```bash
bash tests/test_gates.sh
```

- [ ] **Step 5: Run shellcheck locally if available; fix findings**

```bash
command -v shellcheck >/dev/null && shellcheck scripts/gates.sh tests/test_gates.sh || echo "shellcheck not installed locally; CI covers it"
```

- [ ] **Step 6: Commit**

```bash
git add scripts/gates.sh tests/test_gates.sh
git commit -m "Add run_release_gates entrypoint with tag-version cross-check and GITHUB_OUTPUT"
```

---

### Task 7: Determinism fixture

**Files:**
- Create: `fixtures/demo-pkg/pyproject.toml`
- Create: `fixtures/demo-pkg/demo_pkg/__init__.py`
- Create: `tests/test_determinism.sh`

**Interfaces:**
- Produces: `tests/test_determinism.sh`, exit 0 when two consecutive wheel builds of `fixtures/demo-pkg` are byte-identical. Module CI runs it.

- [ ] **Step 1: Write the fixture package**

`fixtures/demo-pkg/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools==84.0.0"]
build-backend = "setuptools.build_meta"

[project]
name = "demo-pkg"
version = "0.1.0"
description = "Determinism fixture for release-policy CI"
requires-python = ">=3.10"

[tool.setuptools.packages.find]
include = ["demo_pkg*"]
```

`fixtures/demo-pkg/demo_pkg/__init__.py`:

```python
"""Determinism fixture for release-policy CI."""

__all__: list[str] = []
```

- [ ] **Step 2: Write the determinism test**

`tests/test_determinism.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
command -v python3 >/dev/null 2>&1 || PYTHON=python

cd "$HERE/../fixtures/demo-pkg"
rm -rf dist-a dist-b
"$PYTHON" -m build --wheel --outdir dist-a >/dev/null
"$PYTHON" -m build --wheel --outdir dist-b >/dev/null
sum_a="$(sha256sum dist-a/*.whl | awk '{print $1}')"
sum_b="$(sha256sum dist-b/*.whl | awk '{print $1}')"
rm -rf dist-a dist-b demo_pkg.egg-info build
if [ "$sum_a" != "$sum_b" ]; then
  echo "determinism failure: $sum_a != $sum_b" >&2
  exit 1
fi
echo "deterministic wheel build confirmed: $sum_a"
```

- [ ] **Step 3: Run it locally** (requires `pip install "build==1.2.2"` once)

```bash
python -m pip install --quiet "build==1.2.2"
bash tests/test_determinism.sh
```

Expected: `deterministic wheel build confirmed: <digest>`, exit 0.

- [ ] **Step 4: Commit**

```bash
git add fixtures/ tests/test_determinism.sh
git commit -m "Add demo package fixture and wheel determinism test"
```

---

### Task 8: Reusable release workflow

**Files:**
- Create: `.github/workflows/release-python.yml`

**Interfaces:**
- Consumes: `run_release_gates` from `scripts/gates.sh` (Task 6 signature).
- Produces: reusable workflow `release-python.yml` with one optional input `version-command` (string, default `""`). Callers grant `contents: write`, `attestations: write`, `id-token: write`.

- [ ] **Step 1: Write the workflow**

```yaml
name: Reusable Python release

on:
  workflow_call:
    inputs:
      version-command:
        description: >-
          Optional shell command printing the package version, for projects
          whose pyproject.toml declares version as dynamic.
        required: false
        type: string
        default: ""

permissions:
  contents: read

jobs:
  release:
    name: gate, build, attest and publish
    runs-on: ubuntu-latest
    permissions:
      attestations: write # Record provenance and SBOM attestations for release assets.
      contents: write # Create the draft release, upload assets and publish it.
      id-token: write # Obtain the OIDC identity required to sign attestations.
    concurrency:
      group: release-${{ github.repository }}-${{ github.ref }}
      cancel-in-progress: false
    steps:
      - name: Check out the tagged consumer source
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false

      - name: Check out release-policy at the calling pin
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: ryanduguid/release-policy
          ref: ${{ github.job_workflow_sha }}
          path: .release-policy
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.12"

      - name: Install the locked toolchain
        run: python -m pip install "uv==0.12.0"

      - name: Require a release-ready tag and repository
        id: release
        env:
          GH_TOKEN: ${{ github.token }}
          VERSION_COMMAND: ${{ inputs.version-command }}
        shell: bash
        run: |
          set -euo pipefail
          . .release-policy/scripts/gates.sh
          run_release_gates "$GITHUB_REF_NAME" "$GITHUB_SHA" "$GITHUB_REPOSITORY" "$VERSION_COMMAND"

      - name: Run the locked test suite
        run: uv run --locked --extra dev --python 3.12 pytest

      - name: Build wheel and source distribution
        run: uv run --locked --extra dev --python 3.12 python -m build

      - name: Generate SPDX SBOM for the wheel
        uses: anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610 # v0.24.0
        with:
          file: dist/${{ steps.release.outputs.stem }}-${{ steps.release.outputs.version }}-py3-none-any.whl
          format: spdx-json
          syft-version: v1.51.0
          output-file: dist/${{ steps.release.outputs.stem }}-${{ steps.release.outputs.version }}.spdx.json
          upload-artifact: false
          upload-release-assets: false

      - name: Write and verify SHA-256 checksums
        shell: bash
        run: |
          set -euo pipefail
          (cd dist && sha256sum *.whl *.tar.gz *.spdx.json > SHA256SUMS)
          (cd dist && sha256sum --check SHA256SUMS)

      - name: Attest release asset provenance
        uses: actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4.2.2
        with:
          subject-path: |
            dist/*.whl
            dist/*.tar.gz
            dist/*.spdx.json
            dist/SHA256SUMS

      - name: Attest the wheel SBOM
        uses: actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4.2.2
        with:
          subject-path: dist/${{ steps.release.outputs.stem }}-${{ steps.release.outputs.version }}-py3-none-any.whl
          sbom-path: dist/${{ steps.release.outputs.stem }}-${{ steps.release.outputs.version }}.spdx.json

      - name: Publish the complete release
        env:
          GH_TOKEN: ${{ github.token }}
        shell: bash
        run: |
          set -euo pipefail
          gh release create "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY" --verify-tag --draft --title "$GITHUB_REF_NAME" --notes-file RELEASE_NOTES.md
          gh release upload "$GITHUB_REF_NAME" dist/* --repo "$GITHUB_REPOSITORY"
          gh release edit "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY" --draft=false --latest
```

- [ ] **Step 2: Validate with actionlint**

Download the pinned actionlint release, verify against its published checksums, run:

```bash
SCRATCH="C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad"
cd "$(mktemp -d)"
curl -sSLO https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_windows_amd64.zip
curl -sSLO https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_checksums.txt
grep windows_amd64.zip actionlint_1.7.7_checksums.txt | sha256sum --check
unzip -q actionlint_1.7.7_windows_amd64.zip
cp actionlint.exe "$SCRATCH/actionlint.exe"
cd "$SCRATCH/release-policy" && "$SCRATCH/actionlint.exe" .github/workflows/release-python.yml
```

Expected: no findings. If v1.7.7 is no longer the latest, check the latest tag first with `gh api repos/rhysd/actionlint/releases/latest --jq .tag_name` and use that version consistently here and in Task 9.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release-python.yml
git commit -m "Add reusable packaged-Python release workflow"
```

---

### Task 9: Module CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `tests/test_gates.sh`, `tests/test_determinism.sh`, workflow files.
- Produces: CI on push/PR to `main`: shellcheck (runner-provided), actionlint (pinned download, checksum-verified), gate tests, determinism test.

- [ ] **Step 1: Capture the actionlint Linux digest for pinning**

```bash
curl -sSL https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_checksums.txt | grep linux_amd64
```

Record the printed digest; it is pasted into the workflow below as `ACTIONLINT_SHA256`.

- [ ] **Step 2: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  checks:
    name: lint and test
    runs-on: ubuntu-latest
    env:
      ACTIONLINT_VERSION: "1.7.7"
      ACTIONLINT_SHA256: "<digest captured in step 1>"
    steps:
      - name: Check out source
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Shellcheck the scripts and tests
        run: shellcheck scripts/*.sh tests/*.sh

      - name: Download and verify actionlint
        shell: bash
        run: |
          set -euo pipefail
          curl -sSLO "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"
          echo "${ACTIONLINT_SHA256}  actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" | sha256sum --check
          tar -xzf "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" actionlint

      - name: Actionlint the workflows
        run: ./actionlint .github/workflows/*.yml

      - name: Set up Python
        uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.12"

      - name: Run gate tests
        run: bash tests/test_gates.sh

      - name: Run determinism test
        run: |
          set -euo pipefail
          python -m pip install "build==1.2.2"
          bash tests/test_determinism.sh
```

- [ ] **Step 3: Validate ci.yml with the local actionlint from Task 8**

```bash
SCRATCH="C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad"
cd "$SCRATCH/release-policy" && "$SCRATCH/actionlint.exe" .github/workflows/ci.yml
```

Expected: no findings.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "Add module CI: shellcheck, pinned actionlint, gate and determinism tests"
```

---

### Task 10: Consumer documentation

**Files:**
- Modify: `README.md` (replace stub body below the first paragraph)

**Interfaces:**
- Consumes: caller contract from Task 8.
- Produces: the README consumers read before migrating.

- [ ] **Step 1: Write the consumer documentation**

Replace the stub's final line ("Consumer documentation lands...") with:

```markdown
## Using the packaged-Python release workflow

Caller workflow (`.github/workflows/release.yml` in the consumer):

    name: Release
    on:
      push:
        tags: ["v*"]
    permissions:
      contents: read
    jobs:
      release:
        permissions:
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-python.yml@<full-40-char-commit-sha>

Projects whose `pyproject.toml` declares `dynamic = ["version"]` add:

        with:
          version-command: "python -c 'from your_package.version import __version__; print(__version__)'"

## Prerequisites in the consumer

- Annotated tags only; the human-created annotated tag is the release
  approval act. The module never creates tags.
- `RELEASE_NOTES.md` whose first line is `# vX.Y.Z` for the tag.
- `uv.lock` committed; the workflow runs `uv run --locked`.
- `pyproject.toml` with a static `[project] version`, or the
  `version-command` input.

## Policy guarantees

- Fail closed: canonical semver tag, annotated tag object, tag commit equal
  to `main` via the GitHub API, clean tree, matching notes header, and no
  existing release for the tag, all verified before any build.
- Wheel and sdist built from the locked environment after the locked test
  suite passes.
- SPDX SBOM, verified `SHA256SUMS`, provenance and SBOM attestations on
  every asset, draft-then-publish lifecycle.
- Consumers pin this repository by full commit SHA and upgrade by reviewed
  pull request (ADR-0001).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Document the consumer contract and policy guarantees"
```

---

### Task 11: Publish the module repository

**Files:** none (remote operations)

**Interfaces:**
- Produces: `ryanduguid/release-policy` on GitHub; `MODULE_SHA` (the pushed HEAD) used verbatim by Tasks 12-14.

- [ ] **Step 1: Confirm with Ryan before creating the public repository** (outward-facing action)

- [ ] **Step 2: Create and push**

```bash
cd "C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad/release-policy"
git config user.email   # must print ryan@duguid.com.au
gh repo create ryanduguid/release-policy --public --source . --push \
  --description "Shared release policy: reusable workflows and testable release gates (ADR-0001)"
git rev-parse HEAD   # record as MODULE_SHA
```

- [ ] **Step 3: Verify CI is green on GitHub**

```bash
gh run watch --repo ryanduguid/release-policy --exit-status "$(gh run list --repo ryanduguid/release-policy --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Expected: CI concludes success. Fix and push again if not; then re-record `MODULE_SHA`.

- [ ] **Step 4: Hand the v0.1.0 tag to Ryan**

Per ADR-0001 the human creates the annotated tag. Provide Ryan the exact commands and wait:

```bash
git tag -a v0.1.0 -m "release-policy v0.1.0" MODULE_SHA
git push origin v0.1.0
gh release create v0.1.0 --repo ryanduguid/release-policy --verify-tag --title v0.1.0 --notes-file RELEASE_NOTES.md
```

Note: the module cannot dogfood `release-python.yml` (it is not a Python package); v0.1.0 is a notes-only release marker. Full dogfooding arrives in phase 2 with the source-archive workflow. Consumers pin `MODULE_SHA`, not the tag.

---

### Task 12: Pilot migration, au-tax-change-impact-monitor

**Files (in a fresh clone of the consumer):**
- Modify: `.github/workflows/release.yml` (full replacement)

**Interfaces:**
- Consumes: `MODULE_SHA` from Task 11.

- [ ] **Step 1: Clone and branch**

```bash
cd "C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad"
gh repo clone ryanduguid/au-tax-change-impact-monitor
cd au-tax-change-impact-monitor
git config user.name "Ryan Duguid" && git config user.email "ryan@duguid.com.au"
git switch -c adopt-release-policy
```

- [ ] **Step 2: Replace `.github/workflows/release.yml`** (whole file; substitute the recorded 40-char `MODULE_SHA`)

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

permissions:
  contents: read

jobs:
  release:
    permissions:
      attestations: write # Record provenance and SBOM attestations for release assets.
      contents: write # Create the draft release, upload assets and publish it.
      id-token: write # Obtain the OIDC identity required to sign attestations.
    uses: ryanduguid/release-policy/.github/workflows/release-python.yml@MODULE_SHA # v0.1.0
```

- [ ] **Step 3: Sanity-check the consumer prerequisites**

```bash
test -f uv.lock && test -f RELEASE_NOTES.md
python -c 'import tomllib; d=tomllib.load(open("pyproject.toml","rb"))["project"]; print(d["name"], d.get("version", "DYNAMIC"))'
```

Expected: both files exist; output shows a static version (this repo reads version via tomllib today). If it prints `DYNAMIC`, add the `version-command` input as in Task 14.

- [ ] **Step 4: Commit, push, open the PR**

```bash
git add .github/workflows/release.yml
git commit -m "Adopt the shared release-policy reusable workflow

Replaces the repo-local release pipeline with a caller pinned to
ryanduguid/release-policy@MODULE_SHA (v0.1.0). Policy content is
unchanged in effect and hardened to the canonical gate set: semver tag
regex, annotated tag, API main check, clean tree, paginated no-overwrite.
Supersedes the open release-hardening draft. See ADR-0001 in the module
repository."
git push -u origin adopt-release-policy
gh pr create --repo ryanduguid/au-tax-change-impact-monitor \
  --title "Adopt the shared release-policy reusable workflow" \
  --body "Replaces the 100-line repo-local release workflow with a 20-line caller pinned to ryanduguid/release-policy@MODULE_SHA (v0.1.0). Canonical gate set per ADR-0001: https://github.com/ryanduguid/release-policy/blob/main/docs/adr/0001-shared-release-policy.md. Supersedes and closes #18."
```

- [ ] **Step 5: Close the superseded draft with a comment**

```bash
gh pr close 18 --repo ryanduguid/au-tax-change-impact-monitor \
  --comment "Superseded by the shared release-policy module (ADR-0001); the hardened gate set now arrives through the pinned reusable workflow. See the adopt-release-policy PR."
```

---

### Task 13: Pilot migration, monthly-close-control-plane

Same shape as Task 12 with these substitutions; repeated here so the task stands alone.

- [ ] **Step 1: Clone and branch**

```bash
cd "C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad"
gh repo clone ryanduguid/monthly-close-control-plane
cd monthly-close-control-plane
git config user.name "Ryan Duguid" && git config user.email "ryan@duguid.com.au"
git switch -c adopt-release-policy
```

- [ ] **Step 2: Replace `.github/workflows/release.yml`** with exactly the Task 12 Step 2 content (same caller, same `MODULE_SHA`).

- [ ] **Step 3: Sanity-check prerequisites** (same commands as Task 12 Step 3; this repo also reads version via tomllib today, expect a static version)

- [ ] **Step 4: Commit, push, open the PR** (same commit message and PR body as Task 12 Step 4 with `#18` replaced by `#19` and the repo flag `--repo ryanduguid/monthly-close-control-plane`)

- [ ] **Step 5: Close the superseded draft #19**

```bash
gh pr close 19 --repo ryanduguid/monthly-close-control-plane \
  --comment "Superseded by the shared release-policy module (ADR-0001); the hardened gate set now arrives through the pinned reusable workflow. See the adopt-release-policy PR."
```

Leave Dependabot PRs #20 and #21 untouched; they bump codeql-action, unrelated to release policy.

---

### Task 14: Pilot migration, xero-ai-review-gateway (dynamic version)

Same shape as Task 12 plus the `version-command` input.

- [ ] **Step 1: Clone and branch**

```bash
cd "C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad"
gh repo clone ryanduguid/xero-ai-review-gateway
cd xero-ai-review-gateway
git config user.name "Ryan Duguid" && git config user.email "ryan@duguid.com.au"
git switch -c adopt-release-policy
```

- [ ] **Step 2: Replace `.github/workflows/release.yml`** (whole file; substitute `MODULE_SHA`)

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

permissions:
  contents: read

jobs:
  release:
    permissions:
      attestations: write # Record provenance and SBOM attestations for release assets.
      contents: write # Create the draft release, upload assets and publish it.
      id-token: write # Obtain the OIDC identity required to sign attestations.
    uses: ryanduguid/release-policy/.github/workflows/release-python.yml@MODULE_SHA # v0.1.0
    with:
      version-command: "python -c 'from xero_ai_review_gateway.version import __version__; print(__version__)'"
```

- [ ] **Step 3: Sanity-check prerequisites**

```bash
test -f uv.lock && test -f RELEASE_NOTES.md
python -c 'import tomllib; d=tomllib.load(open("pyproject.toml","rb"))["project"]; print(d["name"], d.get("version", "DYNAMIC"))'
```

Expected: `xero-ai-review-gateway DYNAMIC` (confirmed at head `9079691`), which is why the `version-command` input is present.

- [ ] **Step 4: Commit, push, open the PR** (same messages as Task 12 Step 4 with `#18` replaced by `#16` and `--repo ryanduguid/xero-ai-review-gateway`)

- [ ] **Step 5: Close the superseded draft #16**

```bash
gh pr close 16 --repo ryanduguid/xero-ai-review-gateway \
  --comment "Superseded by the shared release-policy module (ADR-0001); the hardened gate set now arrives through the pinned reusable workflow. See the adopt-release-policy PR."
```

---

### Task 15: Verification and wrap-up

**Files:**
- Modify: `docs/superpowers/specs/2026-08-16-release-policy-design.md` (record outcomes of the open questions)

- [ ] **Step 1: Confirm all three pilot PRs are open and their CI is green**

```bash
for r in au-tax-change-impact-monitor monthly-close-control-plane xero-ai-review-gateway; do
  gh pr checks adopt-release-policy --repo "ryanduguid/$r" || true
done
```

Note: consumer CI does not exercise the release workflow itself (it runs on tag push only). Green CI here validates YAML syntax acceptance and unrelated checks.

- [ ] **Step 2: State the validation limit honestly in each PR** (already in the PR body wording) and record it for Ryan: the end-to-end publish path is first exercised by each repo's next real tagged release. Recommend the first post-merge release be watched with `gh run watch`.

- [ ] **Step 3: Update the spec's open questions with the answers**

Open question 1: gateway version is dynamic; the fallback input is exercised (Task 14). Open question 3: superseded drafts were monitor #18, close-plane #19, gateway #16, closed in Tasks 12-14. Open question 2 (fourth packaged-Python repo) stays open for phase 2 scoping.

- [ ] **Step 4: Commit and push the module repo update**

```bash
cd "C:/Users/-/AppData/Local/Temp/claude/C--/d73ac32a-b268-474f-a1ba-c058181baa3f/scratchpad/release-policy"
git add docs/superpowers/specs/2026-08-16-release-policy-design.md
git commit -m "Record pilot outcomes against the spec's open questions"
git push origin main
```

Merging the three pilot PRs is Ryan's decision, not a task in this plan.
