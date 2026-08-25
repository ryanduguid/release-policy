# Skill-pack Release Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a closed, dependency-aware skill-pack verification and release path to `ryanduguid/release-policy`, then use its exact merged commit to recover `ryanduguid/hardhat-ledger` as v0.1.5 without changing any skill or accounting content.

**Architecture:** Keep family-specific verification in thin, read-only adapters. Both the existing source-archive adapter and the new skill-pack adapter call one fresh-checkout publication core. No file, artefact, output, cache, environment mutation or secret crosses from consumer verification to publication. Hardhat Ledger runs the exact skill verifier on pull requests, `main` and tags, while its existing local Verify job remains in place.

**Tech Stack:** Python 3.12 standard library, `unittest`, Bash, Git, GitHub Actions reusable workflows, PowerShell, GitHub CLI, Claude Code CLI and Codex CLI.

**Spec:** [`docs/superpowers/specs/2026-08-25-skill-pack-release-recovery-design.md`](../specs/2026-08-25-skill-pack-release-recovery-design.md)

## Global constraints

- Start the policy implementation from the approved specification and plan commits on top of policy base `e481b2af54678bad4ce945bdee3831f3dbe8f860`. Confirm it again with `git rev-parse e481b2a` before creating the worktree.
- Start the Hardhat change from exact remote `main` commit `2f29bb51957888b1f427be44a7a0866ed4f4f5e5`. If either remote base has moved, stop and review the delta before editing.
- Use isolated worktrees. Do not edit the stale Hardhat branch `repair/pr22-plugin-packaging`.
- Use `apply_patch` for file edits. Preserve unrelated work and never reset or clean a worktree to make the plan easier.
- Do not modify any Hardhat `SKILL.md`, validation card, accounting rule, plugin name or ten-skill inventory.
- Never move, delete, recreate, rerun or manually complete v0.1.4. Its protected annotated tag stays at `2f29bb51957888b1f427be44a7a0866ed4f4f5e5`, and no v0.1.4 release or assets may be created.
- A successful local command does not authorise a push, pull request, merge or tag. Stop at every authority gate below and obtain explicit approval for that exact remote action.
- The policy pull request must merge before the Hardhat caller pin is written. The exact merged policy SHA is an output of that merge gate. Insert that literal 40-character lower-case SHA in both Hardhat workflows and their test constant. Never commit a branch name, tag, symbolic token or provisional SHA.
- Treat each command block as a fresh shell. Re-derive PR numbers, commit SHAs and temporary paths inside every block that uses them; do not depend on an earlier PowerShell variable surviving an approval pause or tool call.
- Run every PowerShell fence in PowerShell 7 with this mandatory preamble, even where it is not repeated below:

  ```powershell
  Set-StrictMode -Version Latest
  $ErrorActionPreference = "Stop"
  $PSNativeCommandUseErrorActionPreference = $true
  ```

  This makes a non-zero native exit stop the step. A command whose failure is the evidence being tested, such as a red TDD run or expected HTTP 404, must run alone or temporarily disable `$PSNativeCommandUseErrorActionPreference`, capture `$LASTEXITCODE`, restore it immediately and validate the exact expected failure.
- Bind every approval to the literal SHA shown in the approval request. After an approval pause, use `Read-Host` to enter that recorded SHA, validate its 40-hex form and compare both local and remote current state with it. Never redefine the approved value from current `HEAD` or the current PR head.
- Candidate artefacts are evidence. Do not overwrite or delete an existing candidate or draft during recovery.
- Use Australian English in prose and preserve exact product names, identifiers and commands.

## File map

Policy repository, `ryanduguid/release-policy`:

- Create `scripts/verify_skills.py`: closed mode guard, tracked-path checks, canonical version check and fixed subprocess sequence.
- Create `tests/test_verify_skills.py`: synthetic Git fixture tests for every verifier branch.
- Create `.github/workflows/publish-archives.yml`: the only privileged common archive publication core.
- Modify `.github/workflows/release-archive.yml`: retain its public contract and read-only unittest job, then call the core.
- Create `.github/workflows/verify-skills.yml`: read-only verifier-only entry point.
- Create `.github/workflows/release-skills.yml`: guard, verifier and publication adapter.
- Modify `tests/test_release_archives.py`: source-adapter compatibility, sibling checkout, signer and candidate-name contracts.
- Create `tests/test_skill_workflows.py`: skill workflow interfaces, DAG, permissions and data-isolation contracts.
- Modify `.github/workflows/ci.yml`, `tests/test_repository_baseline.sh`, `README.md`, `SECURITY.md` and `docs/superpowers/specs/2026-08-21-skill-pack-release-design.md`.

Hardhat repository, `ryanduguid/hardhat-ledger`:

- Modify `.github/workflows/release.yml`, `.github/workflows/verify.yml` and `tests/test_release_workflow.py` for the exact merged policy pin.
- Modify `VERSION`, both concrete nested plugin manifests and the existing manifest-version test for v0.1.5.
- Modify `RELEASE_NOTES.md`, `RELEASING.md`, `README.md`, `docs/consolidation-transition.md` and `docs/releases/v0.1.4.md`.
- Create `docs/releases/v0.1.5.md`.
- Treat `plugins/subcontractor-accounting-skills/skills/`, `validation/cases/` and `.claude/rules/accounting-safety.md` as frozen content.

---

## Task 1: Create the isolated policy implementation worktree

**Files:**

- Read: policy Git metadata and the approved spec and plan
- Create: an isolated Git worktree only

- [ ] **Step 1: Verify the approved local history and remote base**

Run from the current policy checkout:

```powershell
git status --short --branch
git fetch origin main
$expectedPolicyBase = "e481b2af54678bad4ce945bdee3831f3dbe8f860"
$actualPolicyBase = git rev-parse origin/main
if ($actualPolicyBase -cne $expectedPolicyBase) {
    throw "release-policy origin/main moved from the reviewed base"
}
git merge-base --is-ancestor $expectedPolicyBase HEAD
git log --oneline "${expectedPolicyBase}..HEAD"
```

Expected: a clean worktree, `git merge-base` exits zero, and the range contains only the approved design and implementation-plan commits. If not, stop for review.

- [ ] **Step 2: Create a worktree and confirm its exact starting point**

```powershell
$policySource = (Get-Location).Path
$policyWorktree = Join-Path (Split-Path $policySource -Parent) "release-policy-skill-pack-recovery"
if (Test-Path -LiteralPath $policyWorktree) {
    throw "planned policy worktree path already exists: $policyWorktree"
}
git worktree add -b feat/skill-pack-release-recovery $policyWorktree HEAD
git -C $policyWorktree status --short --branch
git -C $policyWorktree rev-parse HEAD
```

Expected: branch `feat/skill-pack-release-recovery`, no working-tree changes and `HEAD` equal to the implementation-plan commit.

---

## Task 2: Build the fixed skill verifier test-first

**Files:**

- Create: `scripts/verify_skills.py`
- Create: `tests/test_verify_skills.py`

- [ ] **Step 1: Add failing tests for the public Python interface**

In `tests/test_verify_skills.py`, import `scripts/verify_skills.py` through the same `sys.path` pattern as `tests/test_release_archives.py`. Create `SkillVerifierTests` with these exact method names:

- `test_guard_accepts_supported_mode_and_rejects_empty_unknown_and_frozen_v010`
- `test_valid_fixture_runs_exact_argument_arrays_in_order_without_shell`
- `test_each_of_four_command_failures_stops_immediately`
- `test_required_files_reject_missing_untracked_symlink_gitlink_and_directory`
- `test_path_rejects_absolute_backslash_traversal_parent_symlink_and_outside_root`
- `test_tests_directory_requires_real_directory_and_tracked_regular_test`
- `test_version_accepts_one_canonical_line_and_rejects_bom_encoding_and_bad_semver`
- `test_cli_returns_nonzero_for_guard_and_verification_errors`

Implement every body with the following exact assertions:

- initialise a temporary Git repository with `git init --quiet`, set local test identity, create the five required files plus `tests/test_example.py`, and stage them with `git add`;
- accept only `subcontractor-accounting-v1` and reject `""`, `unknown` and tag `v0.1.0`;
- record four runner calls and compare their argument tuples to the fixed commands in the approved order;
- assert every call has `cwd` equal to the fixture root, `check=True` and `shell=False`;
- inject failures at indices 0 through 3 and assert no later command is invoked;
- cover Git modes `100644`, `100755`, `120000` and `160000`, plus untracked files and directories in a file position;
- reject `C:/outside`, `/outside`, backslashes, `../outside`, `tests/../VERSION`, a symlinked parent and a resolved path outside the checkout;
- accept `1.2.3`, `1.2.3\n` and `1.2.3\r\n`; reject a UTF-8 BOM, invalid UTF-8, blank, multiple lines, `v1.2.3`, `01.2.3` and `1.2.3.4`;
- invoke `main()` with patched arguments and captured `stderr`, and require a non-zero integer for guard and verification failures.

Use `skipTest` only when the host genuinely cannot create a symlink or gitlink fixture. The Linux CI run must execute those cases.

- [ ] **Step 2: Run the focused test and record the expected red result**

```powershell
python -B -m unittest discover -s tests -p "test_verify_skills.py" -v
```

Expected: failure during import because `verify_skills` does not exist. A syntax error in the test itself is not an acceptable red result.

- [ ] **Step 3: Implement the verifier with this closed interface**

Create `scripts/verify_skills.py` with these names and signatures:

```python
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


SUPPORTED_MODES = frozenset({"subcontractor-accounting-v1"})
REGULAR_GIT_MODES = frozenset({"100644", "100755"})
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
Command = tuple[str, ...]
Runner = Callable[..., subprocess.CompletedProcess[object]]


class VerificationError(ValueError):
    """A skill pack does not satisfy the closed verification contract."""


def require_supported_mode(mode: str) -> None:
    if mode not in SUPPORTED_MODES:
        raise VerificationError(f"unsupported skills verification mode: {mode!r}")


def guard_skill_release(mode: str, tag: str) -> None:
    require_supported_mode(mode)
    if mode == "subcontractor-accounting-v1" and tag == "v0.1.0":
        raise VerificationError("v0.1.0 is frozen and must never be rebuilt or replaced")


def resolve_safe_relative(
    root: Path,
    supplied: str,
    *,
    label: str,
) -> tuple[PurePosixPath, Path]:
    raw_parts = supplied.split("/")
    if (
        not supplied
        or "\\" in supplied
        or re.match(r"^[A-Za-z]:/", supplied)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise VerificationError(f"{label} must be a safe relative POSIX path")
    relative = PurePosixPath(supplied)
    if relative.is_absolute():
        raise VerificationError(f"{label} must be a safe relative POSIX path")
    resolved_root = root.resolve(strict=True)
    cursor = resolved_root
    for part in relative.parts:
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError as error:
            raise VerificationError(f"{label} is missing: {supplied}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError(f"{label} contains a symlink component: {supplied}")
    candidate = cursor.resolve(strict=True)
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise VerificationError(f"{label} resolves outside the consumer checkout")
    return relative, candidate


def tracked_mode(root: Path, relative: PurePosixPath) -> str:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--error-unmatch", "--", relative.as_posix()],
        cwd=root,
        check=False,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 1:
        raise VerificationError(f"required path is not one tracked Git entry: {relative}")
    return lines[0].split(maxsplit=1)[0]


def require_tracked_regular_file(root: Path, supplied: str, *, label: str) -> Path:
    relative, candidate = resolve_safe_relative(root, supplied, label=label)
    if not candidate.is_file():
        raise VerificationError(f"{label} is not a regular file: {supplied}")
    mode = tracked_mode(root, relative)
    if mode not in REGULAR_GIT_MODES:
        raise VerificationError(f"{label} has forbidden Git mode {mode}: {supplied}")
    return candidate


def require_tracked_test_files(root: Path) -> tuple[Path, ...]:
    relative, tests_root = resolve_safe_relative(root, "tests", label="tests directory")
    if not tests_root.is_dir():
        raise VerificationError("tests must be a real directory")
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", relative.as_posix()],
        cwd=root,
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
    )
    names = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    tests = tuple(
        require_tracked_regular_file(root, name, label="test file")
        for name in names
        if PurePosixPath(name).name.startswith("test")
        and PurePosixPath(name).suffix == ".py"
    )
    if not tests:
        raise VerificationError("tests must contain a tracked regular test*.py file")
    return tests


def read_canonical_version(path: Path) -> str:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise VerificationError("version file must be UTF-8") from error
    if text.startswith("\ufeff"):
        raise VerificationError("version file must not contain a BOM")
    match = re.fullmatch(r"(.+?)(?:\r?\n)?", text)
    if match is None or SEMVER.fullmatch(match.group(1)) is None:
        raise VerificationError("version file must contain one canonical MAJOR.MINOR.PATCH line")
    return match.group(1)


def commands_for_mode(mode: str, *, python: str = sys.executable) -> tuple[Command, ...]:
    require_supported_mode(mode)
    return (
        (python, "-m", "pip", "install", "--isolated", "--disable-pip-version-check", "--no-input", "--no-deps", "--requirement", "requirements-test.txt"),
        (python, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"),
        (python, "scripts/validate_validation.py"),
        (python, "tests/verify_skills_cli.py"),
    )


def verify_skill_pack(
    root: Path,
    mode: str,
    version_file: str,
    *,
    runner: Runner = subprocess.run,
) -> None:
    require_supported_mode(mode)
    version_path = require_tracked_regular_file(root, version_file, label="version file")
    require_tracked_regular_file(root, "requirements-test.txt", label="requirements file")
    require_tracked_regular_file(root, "scripts/validate_validation.py", label="validation program")
    require_tracked_regular_file(root, "tests/verify_skills_cli.py", label="Skills CLI program")
    require_tracked_test_files(root)
    read_canonical_version(version_path)
    for command in commands_for_mode(mode):
        runner(command, cwd=root, check=True, shell=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a closed skill-pack release mode.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    guard = subcommands.add_parser("guard-release")
    guard.add_argument("--mode", required=True)
    guard.add_argument("--tag", required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--mode", required=True)
    verify.add_argument("--version-file", default="VERSION")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "guard-release":
            guard_skill_release(arguments.mode, arguments.tag)
        else:
            verify_skill_pack(Path.cwd(), arguments.mode, arguments.version_file)
    except (VerificationError, subprocess.CalledProcessError) as error:
        print(f"verify_skills: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Keep each command as an argument tuple. Do not use `shell=True`, command strings, environment-provided command fragments or a dependency-path input.

- [ ] **Step 4: Run the focused tests and make them green**

```powershell
python -B -m unittest discover -s tests -p "test_verify_skills.py" -v
```

Expected: all eight tests pass. If Windows skips symlink or gitlink construction, record the skip and require the Linux CI run to cover it.

- [ ] **Step 5: Commit the verifier**

```powershell
git add scripts/verify_skills.py tests/test_verify_skills.py
git diff --cached --check
git commit -m "feat: add fixed skill-pack verifier"
```

---

## Task 3: Extract the fresh-checkout publication core test-first

**Files:**

- Create: `.github/workflows/publish-archives.yml`
- Modify: `.github/workflows/release-archive.yml`
- Modify: `tests/test_release_archives.py`
- Modify: `tests/test_repository_baseline.sh`

- [ ] **Step 1: Strengthen the archive workflow tests before extraction**

Update `ReleaseArchiveWorkflowTests` so it reads both workflows and asserts:

- `release-archive.yml` still exposes only `artifact-stem` and `version-file` and still runs `python -B -m unittest discover -s tests -v` in `consumer-tests` with `contents: read`;
- that test job checks the consumer into `consumer` and policy into sibling `policy`, proves `job.workflow_sha` is full lower-case hex and matches policy `HEAD`, then runs the fixed unittest command from `consumer`;
- the adapter release job has `needs: consumer-tests`, no `runs-on`, `steps`, `env`, outputs or secrets, and calls `./.github/workflows/publish-archives.yml`;
- the core exposes only `artifact-stem` and `version-file`, has exactly the three write permissions, and owns the non-cancelling repository-and-ref concurrency group;
- the core checks consumer source out to `consumer` and policy source out to sibling `policy`, verifies `job.workflow_sha`, and invokes policy programs through `../policy` from `consumer`;
- the core contains none of `pip install`, `unittest discover`, `scripts/validate_validation.py`, `tests/verify_skills_cli.py` or another consumer-program invocation, so consumer code cannot run with write or OIDC permissions;
- the signer is exactly `ryanduguid/release-policy/.github/workflows/publish-archives.yml`;
- the candidate artefact name contains `${{ steps.release.outputs.tag }}`, `${{ github.run_id }}` and `${{ github.run_attempt }}`, with `overwrite: false`;
- all four asset paths are below `consumer/dist`, notes come from `consumer/RELEASE_NOTES.md`, and neither workflow contains `.release-policy-verified`, `secrets: inherit` or `gh release upload`.

- [ ] **Step 2: Run the archive test and confirm the intended red result**

```powershell
python -B -m unittest discover -s tests -p "test_release_archives.py" -v
```

Expected: failures for the missing core and the still-inline release job.

- [ ] **Step 3: Create the core by moving the existing privileged lifecycle**

Create `.github/workflows/publish-archives.yml` with this exact shell:

```yaml
name: Internal archive publication core

on:
  workflow_call:
    inputs:
      artifact-stem:
        description: Lower-case hyphenated stem used for all release assets.
        required: true
        type: string
      version-file:
        description: Safe relative path containing one canonical MAJOR.MINOR.PATCH line.
        required: false
        type: string
        default: VERSION

permissions:
  contents: read

jobs:
  publish:
    timeout-minutes: 15
    name: gate, build, attest and publish source archives
    runs-on: ubuntu-latest
    permissions:
      attestations: write
      contents: write
      id-token: write
    concurrency:
      group: release-${{ github.repository }}-${{ github.ref }}
      cancel-in-progress: false
    steps:
      - name: Check out the tagged consumer source
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0
          path: consumer
          persist-credentials: false

      - name: Check out release-policy at the calling pin
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: ryanduguid/release-policy
          ref: ${{ job.workflow_sha }}
          path: policy
          persist-credentials: false
```

Move every remaining step from the current inline `release` job into this `publish` job. Preserve its order and mutation behaviour, and make only these path and identity substitutions:

| Existing core expression | Required extracted-core expression |
|---|---|
| `git -C .release-policy rev-parse HEAD` | `git -C policy rev-parse HEAD` |
| `. .release-policy/scripts/gates.sh` | `. ../policy/scripts/gates.sh`, with `working-directory: consumer` |
| `python .release-policy/scripts/build_release_archives.py` | `python ../policy/scripts/build_release_archives.py`, with `working-directory: consumer` |
| `dist/` in action inputs | `consumer/dist/` |
| release shell steps at repository root | add `working-directory: consumer` and keep their internal `dist/` and `RELEASE_NOTES.md` paths |
| `.release-policy/scripts/find_created_draft_release.py` | `../policy/scripts/find_created_draft_release.py` |
| signer `release-archive.yml` | signer `publish-archives.yml` |
| `${{ steps.release.outputs.tag }}-candidate-assets` | `${{ steps.release.outputs.tag }}-${{ github.run_id }}-${{ github.run_attempt }}-candidate-assets` |

Set `overwrite: false` explicitly on `actions/upload-artifact`. Preserve draft URL and numeric ID binding, digest comparisons, tag and `main` rechecks, exact four-asset inventory, polling bounds, publish-by-ID operation and post-publication verification.

- [ ] **Step 4: Reduce `release-archive.yml` to its family adapter**

Keep its complete existing `workflow_call` input block and fixed unittest command. Adapt `consumer-tests` to the same checkout boundary used by the verifier:

```yaml
      - name: Check out the tagged consumer source
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0
          path: consumer
          persist-credentials: false
      - name: Check out release-policy at the calling pin
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: ryanduguid/release-policy
          ref: ${{ job.workflow_sha }}
          path: policy
          persist-credentials: false
      - name: Require an immutable policy pin
        env:
          MODULE_SHA: ${{ job.workflow_sha }}
        shell: bash
        run: |
          set -euo pipefail
          [[ "$MODULE_SHA" =~ ^[0-9a-f]{40}$ ]] \
            || { echo "job.workflow_sha is not a 40-hex policy commit" >&2; exit 1; }
          test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"
      - name: Run the standard source-archive regression suite
        working-directory: consumer
        run: python -B -m unittest discover -s tests -v
```

Retain `fetch-depth: 0` from the existing source-adapter checkout, and assert it in the compatibility test. Retain the existing Python 3.12 setup between the checkouts and the pin check. Then replace the inline `release` job with:

```yaml
  release:
    needs: consumer-tests
    permissions:
      attestations: write
      contents: write
      id-token: write
    uses: ./.github/workflows/publish-archives.yml
    with:
      artifact-stem: ${{ inputs.artifact-stem }}
      version-file: ${{ inputs.version-file }}
```

Do not add `secrets: inherit`, outputs, artefact downloads or a second checkout to this adapter job.

- [ ] **Step 5: Update the canonical baseline for eight documents**

Add `.github/workflows/publish-archives.yml` to `CANONICAL_SHA256`, replace the changed `release-archive.yml` digest, and change the success line from seven to eight canonical documents. Derive hashes from canonical LF bytes after the workflows are final:

```powershell
$hashProgram = @'
from hashlib import sha256
from pathlib import Path

for supplied in (
    ".github/workflows/release-archive.yml",
    ".github/workflows/publish-archives.yml",
):
    path = Path(supplied)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if text.startswith("\ufeff") or "\r" in text.replace("\r\n", ""):
        raise SystemExit(f"{supplied}: non-canonical encoding or line ending")
    canonical = text.replace("\r\n", "\n").encode("utf-8")
    print(f"{supplied} {sha256(canonical).hexdigest()}")
'@
$hashProgram | python -
```

Insert the printed literal hashes. Do not weaken or remove the adverse-mutation self-tests.

- [ ] **Step 6: Run the archive and baseline tests**

```powershell
python -B -m unittest discover -s tests -p "test_release_archives.py" -v
bash tests/test_repository_baseline.sh
git diff --check
```

Expected: all archive tests pass and the baseline reports eight canonical documents with all 25 adverse variants rejected.

- [ ] **Step 7: Commit the core extraction**

```powershell
git add .github/workflows/publish-archives.yml .github/workflows/release-archive.yml tests/test_release_archives.py tests/test_repository_baseline.sh
git diff --cached --check
git commit -m "refactor: extract archive publication core"
```

---

## Task 4: Add the skill-pack verifier and release adapters test-first

**Files:**

- Create: `.github/workflows/verify-skills.yml`
- Create: `.github/workflows/release-skills.yml`
- Create: `tests/test_skill_workflows.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_repository_baseline.sh`

- [ ] **Step 1: Write static contract tests before the workflows**

In `tests/test_skill_workflows.py`, use standard-library text and regular-expression inspection. Do not add PyYAML to the policy repository. Cover these exact contracts:

1. `verify-skills.yml` accepts only `skills-verification-mode` and optional `version-file`, declares no secrets or outputs, grants only `contents: read`, has a 15-minute verifier job, checks consumer into `consumer` and policy into `policy`, pins the policy checkout to `job.workflow_sha`, uses Python 3.12 and calls `verify_skills.py verify`.
2. `release-skills.yml` accepts exactly `artifact-stem`, `skills-verification-mode` and optional `version-file`, with no custom secrets or outputs.
3. Its `guard` job always runs, calls `verify_skills.py guard-release`, fails for frozen v0.1.0, and both later jobs depend on it.
4. Its `verify` job calls `./.github/workflows/verify-skills.yml` with only `contents: read` and the two verification inputs.
5. Its `publish` job needs both `guard` and `verify`, calls `./.github/workflows/publish-archives.yml`, grants exactly `attestations: write`, `contents: write` and `id-token: write`, and passes only archive inputs.
6. Neither workflow has `secrets: inherit`, cache, download, upload, workflow outputs, an arbitrary command input, release-notes input, builder input, asset-glob input or state hand-off.
7. Both supported adapters use relative same-commit calls and the internal core is not named in consumer examples.

Include a DAG assertion that the complete called workflow cannot succeed through skipped dependants when guard exits non-zero. Test for `needs: guard` on verification and `needs: [guard, verify]` on publication, not just a frozen-tag `if` expression.

- [ ] **Step 2: Run the static contract test and confirm red**

```powershell
python -B -m unittest discover -s tests -p "test_skill_workflows.py" -v
```

Expected: failures because both workflows are missing.

- [ ] **Step 3: Create `verify-skills.yml`**

Use this exact workflow structure:

```yaml
name: Reusable skill-pack verification

on:
  workflow_call:
    inputs:
      skills-verification-mode:
        description: Closed policy-owned skill-pack verification mode.
        required: true
        type: string
      version-file:
        description: Safe relative tracked version file.
        required: false
        type: string
        default: VERSION

permissions:
  contents: read

jobs:
  verify:
    timeout-minutes: 15
    name: verify skill-pack consumer
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Check out consumer source
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          path: consumer
          persist-credentials: false
      - name: Check out release-policy at the calling pin
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: ryanduguid/release-policy
          ref: ${{ job.workflow_sha }}
          path: policy
          persist-credentials: false
      - name: Set up Python
        uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.12"
      - name: Require an immutable policy pin
        env:
          MODULE_SHA: ${{ job.workflow_sha }}
        shell: bash
        run: |
          set -euo pipefail
          [[ "$MODULE_SHA" =~ ^[0-9a-f]{40}$ ]] \
            || { echo "job.workflow_sha is not a 40-hex policy commit" >&2; exit 1; }
          test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"
      - name: Run fixed skill-pack verification
        working-directory: consumer
        env:
          SKILLS_VERIFICATION_MODE: ${{ inputs.skills-verification-mode }}
          VERSION_FILE: ${{ inputs.version-file }}
        run: >-
          python ../policy/scripts/verify_skills.py verify
          --mode "$SKILLS_VERIFICATION_MODE"
          --version-file "$VERSION_FILE"
```

Do not persist credentials, expose outputs or add an artefact or cache step.

- [ ] **Step 4: Create `release-skills.yml`**

Use this exact DAG:

```yaml
name: Reusable skill-pack release

on:
  workflow_call:
    inputs:
      artifact-stem:
        description: Lower-case hyphenated stem used for all release assets.
        required: true
        type: string
      version-file:
        description: Safe relative tracked version file.
        required: false
        type: string
        default: VERSION
      skills-verification-mode:
        description: Closed policy-owned skill-pack verification mode.
        required: true
        type: string

permissions:
  contents: read

jobs:
  guard:
    timeout-minutes: 5
    name: guard skill-pack release mode
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Check out release-policy at the calling pin
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: ryanduguid/release-policy
          ref: ${{ job.workflow_sha }}
          path: policy
          persist-credentials: false
      - name: Require an immutable policy pin
        env:
          MODULE_SHA: ${{ job.workflow_sha }}
        shell: bash
        run: |
          set -euo pipefail
          [[ "$MODULE_SHA" =~ ^[0-9a-f]{40}$ ]] \
            || { echo "job.workflow_sha is not a 40-hex policy commit" >&2; exit 1; }
          test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"
      - name: Set up Python
        uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.12"
      - name: Guard the selected mode and frozen tag
        env:
          RELEASE_TAG: ${{ github.ref_name }}
          SKILLS_VERIFICATION_MODE: ${{ inputs.skills-verification-mode }}
        run: >-
          python policy/scripts/verify_skills.py guard-release
          --mode "$SKILLS_VERIFICATION_MODE"
          --tag "$RELEASE_TAG"

  verify:
    needs: guard
    permissions:
      contents: read
    uses: ./.github/workflows/verify-skills.yml
    with:
      skills-verification-mode: ${{ inputs.skills-verification-mode }}
      version-file: ${{ inputs.version-file }}

  publish:
    needs: [guard, verify]
    permissions:
      attestations: write
      contents: write
      id-token: write
    uses: ./.github/workflows/publish-archives.yml
    with:
      artifact-stem: ${{ inputs.artifact-stem }}
      version-file: ${{ inputs.version-file }}
```

The guard must execute and fail for v0.1.0. Do not express this as an `if` that skips all jobs.

- [ ] **Step 5: Update CI wording and the ten-document baseline**

In `.github/workflows/ci.yml`, update the actionlint comment so it says `job.workflow_sha` is used by the release workflows, without a stale occurrence count. Keep the existing exact ignore regular expression.

Add both new workflows to `CANONICAL_SHA256`, replace hashes for every canonical file changed in this task and change the success line to ten canonical documents. Generate literal canonical-LF hashes with the Task 3 here-string extended to all changed canonical paths.

- [ ] **Step 6: Run the new workflow tests and all policy unit tests**

```powershell
python -B -m unittest discover -s tests -p "test_skill_workflows.py" -v
python -B -m unittest discover -s tests -p "test_*.py" -v
bash tests/test_repository_baseline.sh
git diff --check
```

Expected: all tests pass and the baseline reports ten canonical documents.

- [ ] **Step 7: Commit the adapters**

```powershell
git add .github/workflows/verify-skills.yml .github/workflows/release-skills.yml .github/workflows/ci.yml tests/test_skill_workflows.py tests/test_repository_baseline.sh
git diff --cached --check
git commit -m "feat: add skill-pack release adapters"
```

---

## Task 5: Document the policy contract and run the complete local policy suite

**Files:**

- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `docs/superpowers/specs/2026-08-21-skill-pack-release-design.md`
- Modify: `tests/test_repository_baseline.sh`

- [ ] **Step 1: Update policy documentation**

Add complete `shared-conformance` and `release` caller examples to `README.md`. Follow the repository's existing documentation convention for a full-commit pin, while using `verify-skills.yml` for conformance and `release-skills.yml` for publication. The conformance example grants only `contents: read` and passes only `skills-verification-mode: subcontractor-accounting-v1`. The release example grants the exact three write permissions and passes only `artifact-stem: subcontractor-accounting-skills` plus that same mode. State that the documentation marker must be replaced with a reviewed literal 40-character commit before any consumer workflow is committed. Add a baseline adverse test that rejects a documentation marker inside any canonical policy workflow.

Document:

- the fixed files and four-command order for `subcontractor-accounting-v1`;
- the read-only verifier and exact three-write-permission publication split;
- the absence of secrets, outputs, cache and artefact transfer;
- `publish-archives.yml` as an internal, unsupported direct entry point;
- `publish-archives.yml` as the new signer for consumers that advance their pin;
- unchanged behaviour and inputs for the source-archive family, apart from that intentional signer change;
- the fact that v0.1.0 remains a historical notes-only marker and neither phase has been proven by this repository's own tag.

In `SECURITY.md`, name `publish-archives.yml` as the privileged core and explain that verifier jobs execute reviewed consumer code with `contents: read`, while the core uses fresh sibling checkouts and never executes consumer tests.

In the earlier phase 3 design, change its status to superseded for the initial interface and migration order, and link to the approved 25 August recovery design. Preserve the broader future migration discussion.

- [ ] **Step 2: Refresh changed canonical hashes**

Update the literal `README.md` and `SECURITY.md` hashes in `tests/test_repository_baseline.sh`. Add an adverse mutation that inserts the documentation SHA notation into a workflow `uses` line and require rejection. Adjust the reported adverse-variant count to the actual tuple length.

- [ ] **Step 3: Run every local policy gate**

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
bash tests/test_gates.sh
bash tests/test_repository_baseline.sh
python -m pip install "build==1.2.2"
bash tests/test_determinism.sh
$shellFiles = @(Get-ChildItem -LiteralPath @("scripts", "tests") -Filter "*.sh" -File | Sort-Object FullName | Select-Object -ExpandProperty FullName)
if (-not $shellFiles) { throw "no shell files found for shellcheck" }
shellcheck -x @shellFiles
$workflowFiles = @(Get-ChildItem -LiteralPath ".github\workflows" -Filter "*.yml" -File | Sort-Object FullName | Select-Object -ExpandProperty FullName)
if (-not $workflowFiles) { throw "no workflows found for actionlint" }
$actionlintIgnore = 'property "workflow_sha" is not defined in object type \{check_run_id: number; container: \{id: string; network: string\}; services: \{string => \{id: string; network: string; ports: \{string => string\}\}\}; status: string\}'
actionlint -ignore $actionlintIgnore @workflowFiles
git diff --check
```

Expected: every command exits zero. If `shellcheck` or `actionlint` is unavailable locally, stop and install or use a pinned temporary binary whose digest matches `.github/workflows/ci.yml`; do not omit the check. GitHub CI must later rerun the same versions.

- [ ] **Step 4: Inspect the policy diff and commit the documentation**

```powershell
git diff --stat
git diff -- README.md SECURITY.md docs/superpowers/specs/2026-08-21-skill-pack-release-design.md tests/test_repository_baseline.sh
git add README.md SECURITY.md docs/superpowers/specs/2026-08-21-skill-pack-release-design.md tests/test_repository_baseline.sh
git diff --cached --check
git commit -m "docs: document skill-pack release recovery"
```

---

## Task 6: Review the complete policy change and stop for publication approval

**Files:**

- Review: every policy file changed since `e481b2af54678bad4ce945bdee3831f3dbe8f860`
- Modify: only files needed to resolve validated review findings

- [ ] **Step 1: Re-run verification from a clean committed tree**

```powershell
$expectedPolicyBase = "e481b2af54678bad4ce945bdee3831f3dbe8f860"
git status --short
python -B -m unittest discover -s tests -p "test_*.py" -v
bash tests/test_gates.sh
bash tests/test_repository_baseline.sh
bash tests/test_determinism.sh
$shellFiles = @(Get-ChildItem -LiteralPath @("scripts", "tests") -Filter "*.sh" -File | Sort-Object FullName | Select-Object -ExpandProperty FullName)
if (-not $shellFiles) { throw "no shell files found for shellcheck" }
shellcheck -x @shellFiles
$workflowFiles = @(Get-ChildItem -LiteralPath ".github\workflows" -Filter "*.yml" -File | Sort-Object FullName | Select-Object -ExpandProperty FullName)
if (-not $workflowFiles) { throw "no workflows found for actionlint" }
$actionlintIgnore = 'property "workflow_sha" is not defined in object type \{check_run_id: number; container: \{id: string; network: string\}; services: \{string => \{id: string; network: string; ports: \{string => string\}\}\}; status: string\}'
actionlint -ignore $actionlintIgnore @workflowFiles
git diff --check "${expectedPolicyBase}..HEAD"
```

Expected: the tree is clean and all commands pass.

- [ ] **Step 2: Invoke `superpowers:requesting-code-review`**

Give the reviewer the approved spec, this plan, base SHA and head SHA. Require separate Standards and Spec findings, with special attention to:

- consumer-controlled code reaching write or OIDC permissions;
- path traversal, symlink, Git mode and pre-command validation gaps;
- skipped-success frozen-tag behaviour;
- nested permission propagation and secret or state transfer;
- sibling checkout and working-directory mistakes;
- candidate overwrite, draft-ID binding, signer identity and changed source-adapter semantics.

Resolve every valid finding test-first, rerun the complete suite and commit each coherent correction. Do not accept a finding merely because it sounds plausible.

- [ ] **Step 3: Present the exact reviewed state and stop**

Report the branch, base, literal reviewed head SHA, commits, changed files, review verdict and all local command results. State that approval applies only to that literal SHA, then ask for explicit approval to push the policy branch and open its pull request. Do not push or call a GitHub mutation before that approval.

---

## Task 7: Publish and merge the policy pull request through separate gates

**Files:**

- No new local file is planned unless review or CI reveals a defect
- Remote: policy branch and pull request, only after explicit approvals

- [ ] **Step 1: After push and pull-request approval, publish only the reviewed head**

```powershell
$approvedPolicyHead = Read-Host "Enter the exact policy head SHA approved for branch publication"
if ($approvedPolicyHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved policy head is not full lower-case hex" }
$policyHead = git rev-parse HEAD
if ($policyHead -cne $approvedPolicyHead) { throw "local policy HEAD differs from the approved publication SHA" }
git status --short
git push -u origin feat/skill-pack-release-recovery
$remotePolicyHead = git ls-remote origin refs/heads/feat/skill-pack-release-recovery | ForEach-Object { ($_ -split "`t")[0] }
if ($remotePolicyHead -cne $approvedPolicyHead) { throw "remote policy branch does not match approved publication SHA" }
```

Use `apply_patch` to prepare a temporary pull-request body file under the system temporary directory, with sections for incident, trust boundary, tests, compatibility, waiver and authority gates. Name it by appending the literal approved policy head SHA to `release-policy-pr-`; the command below validates the same SHA and resolves that filename. Then run:

```powershell
$approvedPolicyHead = Read-Host "Enter the exact policy head SHA approved for pull-request creation"
if ($approvedPolicyHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved policy head is not full lower-case hex" }
$policyHead = git rev-parse HEAD
if ($policyHead -cne $approvedPolicyHead) { throw "local policy HEAD differs from the approved pull-request SHA" }
$remotePolicyHead = git ls-remote origin refs/heads/feat/skill-pack-release-recovery | ForEach-Object { ($_ -split "`t")[0] }
if ($remotePolicyHead -cne $approvedPolicyHead) { throw "remote policy branch differs from the approved pull-request SHA" }
$policyPrBody = Join-Path ([IO.Path]::GetTempPath()) "release-policy-pr-$approvedPolicyHead.md"
if (-not (Test-Path -LiteralPath $policyPrBody -PathType Leaf)) { throw "reviewed policy PR body is missing" }
$policyPrUrl = (gh pr create --repo ryanduguid/release-policy --base main --head feat/skill-pack-release-recovery --title "Add the skill-pack release adapter" --body-file $policyPrBody).Trim()
$createdPolicyPrHead = gh pr view $policyPrUrl --repo ryanduguid/release-policy --json headRefOid --jq '.headRefOid'
if ($createdPolicyPrHead -cne $approvedPolicyHead) { throw "created policy PR does not use the approved head SHA" }
```

`$policyPrBody` must be the resolved path of that reviewed temporary file. Do not put memory citations or secrets in it.

- [ ] **Step 2: Wait for exact-head CI and review**

```powershell
$approvedPolicyHead = Read-Host "Enter the exact policy head SHA approved for branch publication"
if ($approvedPolicyHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved policy head is not full lower-case hex" }
$policyHead = git rev-parse HEAD
if ($policyHead -cne $approvedPolicyHead) { throw "local policy HEAD differs from the approved publication SHA" }
$policyPr = gh pr view --repo ryanduguid/release-policy --json number,headRefOid --jq '.number'
$policyPrHead = gh pr view $policyPr --repo ryanduguid/release-policy --json headRefOid --jq '.headRefOid'
if ($policyPrHead -cne $approvedPolicyHead) { throw "policy PR head changed after publication approval" }
gh pr checks $policyPr --repo ryanduguid/release-policy --watch
```

Require CI, CodeQL Python and CodeQL Actions to finish successfully on `$policyHead`. Re-read the PR diff and mergeability. If any corrective commit is needed, return to Task 6 review and obtain a new merge decision.

- [ ] **Step 3: Stop for separate merge approval**

Present the PR URL, literal exact head SHA, reviews and terminal checks. State that merge approval applies only to that literal SHA. Do not merge based on the earlier push approval.

- [ ] **Step 4: After merge approval, merge with an exact-head guard and record the policy SHA**

```powershell
$approvedPolicyHead = Read-Host "Enter the exact policy PR head SHA approved for merge"
if ($approvedPolicyHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved policy merge head is not full lower-case hex" }
$policyHead = git rev-parse HEAD
if ($policyHead -cne $approvedPolicyHead) { throw "local policy HEAD differs from the approved merge SHA" }
$policyPr = gh pr view --repo ryanduguid/release-policy --json number --jq '.number'
$currentPolicyPrHead = gh pr view $policyPr --repo ryanduguid/release-policy --json headRefOid --jq '.headRefOid'
if ($currentPolicyPrHead -cne $approvedPolicyHead) { throw "policy PR head no longer matches approved merge SHA" }
gh pr merge $policyPr --repo ryanduguid/release-policy --squash --match-head-commit $approvedPolicyHead
$policySha = gh pr view $policyPr --repo ryanduguid/release-policy --json mergeCommit --jq '.mergeCommit.oid'
if ($policySha -cnotmatch '^[0-9a-f]{40}$') { throw "merged policy SHA is not full lower-case hex" }
$policyMain = gh api repos/ryanduguid/release-policy/commits/main --jq '.sha'
if ($policyMain -cne $policySha) { throw "policy main moved before post-merge acceptance" }
gh pr view $policyPr --repo ryanduguid/release-policy --json state,mergedAt,mergeCommit --jq '{state,mergedAt,mergeCommit}'
```

Wait for post-merge CI and CodeQL on `$policySha`. Preserve `$policySha` as the only permitted Hardhat pin.

---

## Task 8: Create the Hardhat worktree and adopt the exact merged adapters test-first

**Files:**

- Modify: `.github/workflows/release.yml`
- Modify: `.github/workflows/verify.yml`
- Modify: `tests/test_release_workflow.py`

- [ ] **Step 1: Confirm the frozen base and create an isolated worktree**

Run from the existing Hardhat checkout after Task 7 has produced `$policySha`:

```powershell
$policyPrData = @(gh pr list --repo ryanduguid/release-policy --state merged --head feat/skill-pack-release-recovery --limit 2 --json number,mergeCommit | ConvertFrom-Json)
if ($policyPrData.Count -ne 1) { throw "could not identify exactly one merged policy recovery PR" }
$policySha = $policyPrData[0].mergeCommit.oid
if ($policySha -cnotmatch '^[0-9a-f]{40}$') { throw "merged policy SHA is not full lower-case hex" }
git fetch origin main --tags
$expectedHardhatBase = "2f29bb51957888b1f427be44a7a0866ed4f4f5e5"
$actualHardhatBase = git rev-parse origin/main
if ($actualHardhatBase -cne $expectedHardhatBase) {
    throw "hardhat-ledger origin/main moved from the reviewed recovery base"
}
$hardhatSource = (Get-Location).Path
$hardhatWorktree = Join-Path (Split-Path $hardhatSource -Parent) "hardhat-ledger-v0.1.5-recovery"
if (Test-Path -LiteralPath $hardhatWorktree) {
    throw "planned Hardhat worktree path already exists: $hardhatWorktree"
}
git worktree add -b fix/v0.1.5-release-recovery $hardhatWorktree origin/main
git -C $hardhatWorktree status --short --branch
```

Expected: a clean branch at exact `$expectedHardhatBase`. If `main` moved, stop and review before selecting a new base.

- [ ] **Step 2: Expand the two existing workflow tests without changing the test count**

In `tests/test_release_workflow.py`, add `import re` and a module constant whose value is the literal `$policySha` returned by Task 7. Form the exact line first so no symbolic value enters the file:

```powershell
$policyPrData = @(gh pr list --repo ryanduguid/release-policy --state merged --head feat/skill-pack-release-recovery --limit 2 --json mergeCommit | ConvertFrom-Json)
if ($policyPrData.Count -ne 1) { throw "could not identify exactly one merged policy recovery PR" }
$policySha = $policyPrData[0].mergeCommit.oid
$expectedPolicyConstant = 'EXPECTED_POLICY_SHA = "' + $policySha + '"'
Write-Output $expectedPolicyConstant
```

Copy the printed line exactly, then add this regular expression:

```python
POLICY_CALL = re.compile(
    r"ryanduguid/release-policy/\.github/workflows/"
    r"(?P<workflow>verify-skills|release-skills)\.yml@(?P<sha>[0-9a-f]{40})"
)
```

Assert `re.fullmatch(r"[0-9a-f]{40}", EXPECTED_POLICY_SHA)` before using it in comparisons.

Rename the first existing method to `test_workflows_use_the_exact_shared_skill_policy` and make it assert:

- the frozen v0.1.0 condition and error remain;
- `release-archive.yml` is absent;
- release calls `release-skills.yml` and Verify calls `verify-skills.yml` at exactly `EXPECTED_POLICY_SHA`;
- the two extracted pins are identical;
- the release `with` map is exactly `artifact-stem` plus `skills-verification-mode`;
- Verify still triggers on pull requests and `main` pushes, and its jobs are exactly `verify` and `shared-conformance`.

Expand the second existing method so parsed YAML asserts:

- release has no `steps`, `runs-on`, `env`, outputs or secrets and has exactly the three write permissions;
- shared conformance has no `steps`, `runs-on`, `env`, outputs or secrets;
- shared conformance has `contents: read` and only `skills-verification-mode: subcontractor-accounting-v1` in `with`;
- the existing local `verify` job still has checkout, Python 3.12, dependency install, unittest, fabricated validation and Skills CLI steps.

Do not add a third test method. The repository total must remain 43.

- [ ] **Step 3: Run the focused tests and confirm red**

```powershell
python -B -m unittest discover -s tests -p "test_release_workflow.py" -v
```

Expected: both existing tests fail because the workflows still call the source adapter and lack shared conformance.

- [ ] **Step 4: Patch the two workflows using the literal merged SHA**

Create the two exact `uses` lines from the Task 7 value and print them for the patch:

```powershell
$policyPrData = @(gh pr list --repo ryanduguid/release-policy --state merged --head feat/skill-pack-release-recovery --limit 2 --json mergeCommit | ConvertFrom-Json)
if ($policyPrData.Count -ne 1) { throw "could not identify exactly one merged policy recovery PR" }
$policySha = $policyPrData[0].mergeCommit.oid
$releasePolicyCall = "uses: ryanduguid/release-policy/.github/workflows/release-skills.yml@$policySha"
$verifyPolicyCall = "uses: ryanduguid/release-policy/.github/workflows/verify-skills.yml@$policySha"
Write-Output $releasePolicyCall
Write-Output $verifyPolicyCall
```

Replace the release caller with the printed release line in this structure:

```yaml
  release:
    if: github.ref_name != 'v0.1.0'
    permissions:
      attestations: write
      contents: write
      id-token: write
    with:
      artifact-stem: subcontractor-accounting-skills
      skills-verification-mode: subcontractor-accounting-v1
```

Insert the printed `uses` line between `id-token` and `with`, preserving its indentation.

Leave the existing local `verify` job byte-for-byte unchanged and append:

```yaml
  shared-conformance:
    name: shared conformance
    permissions:
      contents: read
    with:
      skills-verification-mode: subcontractor-accounting-v1
```

Insert the printed verifier `uses` line between `contents` and `with`, preserving its indentation. Do not add `secrets: inherit`.

- [ ] **Step 5: Run focused tests and commit the caller change**

```powershell
python -B -m unittest discover -s tests -p "test_release_workflow.py" -v
git diff --check
git add .github/workflows/release.yml .github/workflows/verify.yml tests/test_release_workflow.py
git commit -m "ci: adopt skill-pack release policy"
```

Expected: both workflow tests pass and both pins equal the merged policy SHA.

---

## Task 9: Prepare v0.1.5 metadata and recovery documentation test-first

**Files:**

- Modify: `tests/test_plugin_manifests.py`
- Modify: `VERSION`
- Modify: `plugins/subcontractor-accounting-skills/.claude-plugin/plugin.json`
- Modify: `plugins/subcontractor-accounting-skills/.codex-plugin/plugin.json`
- Modify: `RELEASE_NOTES.md`
- Modify: `RELEASING.md`
- Modify: `README.md`
- Modify: `docs/consolidation-transition.md`
- Modify: `docs/releases/v0.1.4.md`
- Create: `docs/releases/v0.1.5.md`

- [ ] **Step 1: Strengthen the existing version test before metadata changes**

Inside `test_concrete_manifest_versions_match_the_release_version`, add these assertions without creating another test method:

```python
self.assertEqual(version, "0.1.5")
recovery_notes = (REPOSITORY / "docs" / "releases" / "v0.1.5.md").read_text(
    encoding="utf-8"
)
self.assertIn("2f29bb51957888b1f427be44a7a0866ed4f4f5e5", recovery_notes)
self.assertIn("32839062910", recovery_notes)
self.assertIn("No skill or accounting content changed", recovery_notes)
```

Retain the existing checks that both concrete manifest versions match `VERSION` and both marketplaces remain unversioned.

- [ ] **Step 2: Run the focused test and confirm red**

```powershell
python -B -m unittest discover -s tests -p "test_plugin_manifests.py" -v
```

Expected: the existing method fails because version is 0.1.4 and the v0.1.5 document does not exist.

- [ ] **Step 3: Advance only concrete release metadata**

Set `VERSION` and the `version` field in both nested concrete manifests to `0.1.5`. Do not add a version to either marketplace JSON.

Make `RELEASE_NOTES.md` start with this substance:

```markdown
# v0.1.5

`v0.1.5` is a release-process recovery for the protected v0.1.4 tag. Release run 32839062910 stopped in its read-only consumer-test job because the source-archive adapter did not install the tracked PyYAML test dependency. Publication never started, and no v0.1.4 release or assets exist.

## Changes since v0.1.4

- use the dedicated, full-commit-pinned skill-pack release adapter;
- run the same dependency-aware conformance path on pull requests, `main` and the release tag; and
- retain isolated consumer testing and a fresh privileged publication checkout.

No skill or accounting content changed. The plugin name, ten-skill inventory, validation cards, accounting rules and professional-review boundary are unchanged from protected tag commit `2f29bb51957888b1f427be44a7a0866ed4f4f5e5`.
```

Retain the existing included-skills and scope sections, update integrity wording to the new core signer, and list exactly these four expected assets:

```text
subcontractor-accounting-skills-0.1.5.zip
subcontractor-accounting-skills-0.1.5.tar.gz
subcontractor-accounting-skills-0.1.5.spdx.json
SHA256SUMS
```

Create `docs/releases/v0.1.5.md` with the same incident facts, recovery-only scope, exact asset list and unchanged-content statement. Update `docs/releases/v0.1.4.md` to label v0.1.4 as failed-tag evidence, with run 32839062910, the protected target and the absence of publication.

- [ ] **Step 4: Correct all release guidance**

Update `RELEASING.md` so it:

- names `release-skills.yml` as the supported skill-pack entry point and `publish-archives.yml` as the attestation signer;
- runs exactly install, unittest, fabricated validation and Skills CLI in the approved order;
- uses v0.1.5 in every tag and asset command;
- checks before tagging that v0.1.4 still peels to `2f29bb51957888b1f427be44a7a0866ed4f4f5e5`, its release endpoint is 404, and both v0.1.5 tag and release are absent;
- requires the exact policy SHA, Hardhat SHA and exact-head checks before tag approval;
- verifies all four provenance attestations, both archive SPDX attestations, exact notes and asset inventory, checksums, server digests, release and per-asset verification, immutable/latest state and the unchanged v0.1.4 invariant;
- forbids a workflow rerun or manual upload for v0.1.4.

Update `README.md` to link v0.1.5 notes and describe the dependency-aware shared verifier. Change phase 1 in `docs/consolidation-transition.md` from the stale instruction to publish v0.1.4 to the verified v0.1.5 recovery gate. Keep consolidation, deprecation and archival as separate future decisions.

- [ ] **Step 5: Run focused tests and prove content freeze**

```powershell
python -B -m unittest discover -s tests -p "test_plugin_manifests.py" -v
git diff --exit-code 2f29bb51957888b1f427be44a7a0866ed4f4f5e5 -- plugins/subcontractor-accounting-skills/skills validation/cases .claude/rules/accounting-safety.md
git diff --check
```

Expected: manifest tests pass and the frozen-content diff is empty.

- [ ] **Step 6: Commit the recovery metadata**

```powershell
git add VERSION plugins/subcontractor-accounting-skills/.claude-plugin/plugin.json plugins/subcontractor-accounting-skills/.codex-plugin/plugin.json RELEASE_NOTES.md RELEASING.md README.md docs/consolidation-transition.md docs/releases/v0.1.4.md docs/releases/v0.1.5.md tests/test_plugin_manifests.py
git diff --cached --check
git commit -m "chore: prepare v0.1.5 recovery"
```

---

## Task 10: Run complete Hardhat acceptance and isolated runtime installs

**Files:**

- Read and execute: repository tests, validators and manifests
- Create: temporary virtual environment and temporary CLI state outside the repository
- Modify: no repository file unless a validated failure requires a test-first fix

- [ ] **Step 1: Run the exact shared verifier command order in a fresh Python 3.12 environment**

```powershell
$hardhatHead = git rev-parse HEAD
$acceptanceRoot = Join-Path ([IO.Path]::GetTempPath()) "hardhat-v015-acceptance-$hardhatHead"
if (Test-Path -LiteralPath $acceptanceRoot) { throw "Hardhat acceptance root already exists" }
New-Item -ItemType Directory -Path $acceptanceRoot | Out-Null
$testVenv = Join-Path $acceptanceRoot "tests"
py -3.12 -m venv $testVenv
$testPython = Join-Path $testVenv "Scripts\python.exe"
& $testPython -m pip install --isolated --disable-pip-version-check --no-input --no-deps --requirement requirements-test.txt
& $testPython -B -m unittest discover -s tests -v
& $testPython scripts/validate_validation.py
& $testPython tests/verify_skills_cli.py
```

Expected: 43 unit tests, nine fabricated validation cards covering exactly ten skills, and `skills@1.5.22` discovering exactly ten skills.

- [ ] **Step 2: Run strict marketplace validation**

```powershell
claude plugin validate --strict .claude-plugin/marketplace.json
git diff --check
```

Expected: strict validation succeeds and the worktree remains clean.

- [ ] **Step 3: Install through Claude and Codex without touching normal user state**

Use child-process environment maps. Anthropic documents that `CLAUDE_CONFIG_DIR` relocates all settings, credentials, history and plugins. Current OpenAI Codex source resolves `CODEX_HOME` for plugin operations and reports the installed version and path in JSON. See:

- <https://code.claude.com/docs/en/env-vars>
- <https://github.com/openai/codex/blob/main/codex-rs/cli/src/plugin_cmd.rs>

Run this helper and acceptance sequence:

```powershell
function Invoke-IsolatedCli {
    param(
        [Parameter(Mandatory)] [string] $File,
        [Parameter(Mandatory)] [string[]] $Arguments,
        [Parameter(Mandatory)] [hashtable] $Environment
    )
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $File
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in $Arguments) { [void] $start.ArgumentList.Add($argument) }
    foreach ($entry in $Environment.GetEnumerator()) {
        $start.Environment[$entry.Key] = [string] $entry.Value
    }
    $process = [Diagnostics.Process]::Start($start)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) {
        throw "$File failed with exit code $($process.ExitCode): $stderr"
    }
    return $stdout
}

$repoRoot = (Get-Location).Path
$hardhatHead = git rev-parse HEAD
$acceptanceRoot = Join-Path ([IO.Path]::GetTempPath()) "hardhat-v015-acceptance-$hardhatHead"
if (-not (Test-Path -LiteralPath (Join-Path $acceptanceRoot "tests"))) { throw "isolated test environment evidence is missing" }
$claudeTestRoot = Join-Path $acceptanceRoot "claude"
$codexTestRoot = Join-Path $acceptanceRoot "codex"
New-Item -ItemType Directory -Path $claudeTestRoot, $codexTestRoot | Out-Null

$claudeEnvironment = @{ CLAUDE_CONFIG_DIR = $claudeTestRoot }
Invoke-IsolatedCli -File "claude" -Arguments @("plugin", "marketplace", "add", $repoRoot, "--scope", "user") -Environment $claudeEnvironment | Out-Null
Invoke-IsolatedCli -File "claude" -Arguments @("plugin", "install", "subcontractor-accounting-skills@ryanduguid-contracting", "--scope", "user") -Environment $claudeEnvironment | Out-Null
$claudeList = Invoke-IsolatedCli -File "claude" -Arguments @("plugin", "list", "--json") -Environment $claudeEnvironment
if ($claudeList -notmatch 'subcontractor-accounting-skills') { throw "isolated Claude install is absent" }
$claudeManifests = Get-ChildItem -LiteralPath $claudeTestRoot -Recurse -Filter plugin.json -File
$claudeMatch = $claudeManifests | Where-Object {
    $manifest = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
    $manifest.name -eq "subcontractor-accounting-skills" -and $manifest.version -eq "0.1.5"
}
if (@($claudeMatch).Count -ne 1) { throw "isolated Claude install did not resolve exactly one v0.1.5 manifest" }

$codexEnvironment = @{ CODEX_HOME = $codexTestRoot }
Invoke-IsolatedCli -File "codex" -Arguments @("plugin", "marketplace", "add", $repoRoot, "--json") -Environment $codexEnvironment | Out-Null
$codexInstall = Invoke-IsolatedCli -File "codex" -Arguments @("plugin", "add", "subcontractor-accounting-skills@ryanduguid-contracting", "--json") -Environment $codexEnvironment | ConvertFrom-Json
if ($codexInstall.version -ne "0.1.5") { throw "isolated Codex install version mismatch" }
$installedPath = [IO.Path]::GetFullPath($codexInstall.installedPath)
$expectedCodexRoot = [IO.Path]::GetFullPath($codexTestRoot)
$relativeInstalledPath = [IO.Path]::GetRelativePath($expectedCodexRoot, $installedPath)
$parentPrefix = "..$([IO.Path]::DirectorySeparatorChar)"
$installedOutsideRoot = ($relativeInstalledPath -eq ".") -or ($relativeInstalledPath -eq "..") -or [IO.Path]::IsPathRooted($relativeInstalledPath) -or $relativeInstalledPath.StartsWith($parentPrefix, [StringComparison]::OrdinalIgnoreCase)
if ($installedOutsideRoot) {
    throw "Codex installed outside isolated CODEX_HOME"
}
$codexList = Invoke-IsolatedCli -File "codex" -Arguments @("plugin", "list", "--json") -Environment $codexEnvironment
if ($codexList -notmatch 'subcontractor-accounting-skills') { throw "isolated Codex install is absent" }
```

Do not set process-global `CLAUDE_CONFIG_DIR` or `CODEX_HOME`, and do not reuse those names as shell variables.

- [ ] **Step 4: Remove only the verified temporary roots**

```powershell
$hardhatHead = git rev-parse HEAD
$acceptanceRoot = Join-Path ([IO.Path]::GetTempPath()) "hardhat-v015-acceptance-$hardhatHead"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fullRoot = [IO.Path]::GetFullPath($acceptanceRoot)
$leaf = [IO.Path]::GetFileName($fullRoot)
$relativeRoot = [IO.Path]::GetRelativePath($tempBase, $fullRoot)
$parentPrefix = "..$([IO.Path]::DirectorySeparatorChar)"
$outsideTemp = ($relativeRoot -eq ".") -or ($relativeRoot -eq "..") -or [IO.Path]::IsPathRooted($relativeRoot) -or $relativeRoot.StartsWith($parentPrefix, [StringComparison]::OrdinalIgnoreCase)
if ($outsideTemp) {
    throw "refusing to remove a path outside the system temporary directory"
}
if ($leaf -cnotmatch '^hardhat-v015-acceptance-[0-9a-f]{40}$') {
    throw "refusing to remove an unexpected temporary path"
}
Remove-Item -LiteralPath $fullRoot -Recurse -Force
```

Report that these disposable validation directories were removed. They contain only synthetic CLI state and the temporary test environment.

- [ ] **Step 5: Prove frozen content and a clean committed tree again**

```powershell
git diff --exit-code 2f29bb51957888b1f427be44a7a0866ed4f4f5e5 -- plugins/subcontractor-accounting-skills/skills validation/cases .claude/rules/accounting-safety.md
git status --short
git log --oneline 2f29bb51957888b1f427be44a7a0866ed4f4f5e5..HEAD
```

Expected: frozen-content diff empty, worktree clean, and exactly the intended caller and v0.1.5 commits.

---

## Task 11: Review and publish the Hardhat pull request through separate gates

**Files:**

- Review: every Hardhat file changed from `2f29bb51957888b1f427be44a7a0866ed4f4f5e5`
- Modify: only files needed to resolve validated review findings

- [ ] **Step 1: Invoke `superpowers:requesting-code-review`**

Give the reviewer the approved recovery spec, this plan, Hardhat base and head, and exact merged policy SHA. Require Standards and Spec findings covering:

- identical literal policy pins and exact adapter paths;
- preservation of the local Verify job and frozen v0.1.0 refusal;
- release privilege map and absence of local privileged steps or secrets;
- exact v0.1.5 metadata and unversioned marketplaces;
- truthfulness of incident, run and protected-tag documentation;
- byte-for-byte frozen skill, validation and accounting content.

Fix valid findings test-first, rerun Task 10 and commit coherent corrections.

- [ ] **Step 2: Stop for approval to push and open the Hardhat pull request**

Present the literal exact head SHA, policy pin, commits, 43-test result, nine-card result, ten-skill discovery, strict marketplace validation, both isolated installs, frozen-content proof and review verdict. State that approval applies only to that literal SHA. Do not push yet.

- [ ] **Step 3: After approval, push and open the exact reviewed head**

```powershell
$approvedHardhatHead = Read-Host "Enter the exact Hardhat head SHA approved for branch publication"
if ($approvedHardhatHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved Hardhat head is not full lower-case hex" }
$hardhatHead = git rev-parse HEAD
if ($hardhatHead -cne $approvedHardhatHead) { throw "local Hardhat HEAD differs from the approved publication SHA" }
git push -u origin fix/v0.1.5-release-recovery
$remoteHardhatHead = git ls-remote origin refs/heads/fix/v0.1.5-release-recovery | ForEach-Object { ($_ -split "`t")[0] }
if ($remoteHardhatHead -cne $approvedHardhatHead) { throw "remote Hardhat branch does not match approved publication SHA" }
```

Use `apply_patch` to prepare a temporary body file under the system temporary directory that records the v0.1.4 incident, unchanged-content scope, policy SHA and acceptance evidence. Name it by appending the literal approved Hardhat head SHA to `hardhat-ledger-pr-`; the command below validates the same SHA and resolves that filename. Then run:

```powershell
$approvedHardhatHead = Read-Host "Enter the exact Hardhat head SHA approved for pull-request creation"
if ($approvedHardhatHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved Hardhat head is not full lower-case hex" }
$hardhatHead = git rev-parse HEAD
if ($hardhatHead -cne $approvedHardhatHead) { throw "local Hardhat HEAD differs from the approved pull-request SHA" }
$remoteHardhatHead = git ls-remote origin refs/heads/fix/v0.1.5-release-recovery | ForEach-Object { ($_ -split "`t")[0] }
if ($remoteHardhatHead -cne $approvedHardhatHead) { throw "remote Hardhat branch differs from the approved pull-request SHA" }
$hardhatPrBody = Join-Path ([IO.Path]::GetTempPath()) "hardhat-ledger-pr-$approvedHardhatHead.md"
if (-not (Test-Path -LiteralPath $hardhatPrBody -PathType Leaf)) { throw "reviewed Hardhat PR body is missing" }
$hardhatPrUrl = (gh pr create --repo ryanduguid/hardhat-ledger --base main --head fix/v0.1.5-release-recovery --title "Recover the protected v0.1.4 release as v0.1.5" --body-file $hardhatPrBody).Trim()
$createdHardhatPrHead = gh pr view $hardhatPrUrl --repo ryanduguid/hardhat-ledger --json headRefOid --jq '.headRefOid'
if ($createdHardhatPrHead -cne $approvedHardhatHead) { throw "created Hardhat PR does not use the approved head SHA" }
```

`$hardhatPrBody` must be the resolved path of that reviewed temporary file.

- [ ] **Step 4: Require exact-head pull-request checks**

```powershell
$approvedHardhatHead = Read-Host "Enter the exact Hardhat head SHA approved for branch publication"
if ($approvedHardhatHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved Hardhat head is not full lower-case hex" }
$hardhatHead = git rev-parse HEAD
if ($hardhatHead -cne $approvedHardhatHead) { throw "local Hardhat HEAD differs from the approved publication SHA" }
$hardhatPr = gh pr view --repo ryanduguid/hardhat-ledger --json number --jq '.number'
$hardhatPrHead = gh pr view $hardhatPr --repo ryanduguid/hardhat-ledger --json headRefOid --jq '.headRefOid'
if ($hardhatPrHead -cne $approvedHardhatHead) { throw "Hardhat PR head changed after publication approval" }
gh pr checks $hardhatPr --repo ryanduguid/hardhat-ledger --watch
```

Require local `verify`, `shared-conformance`, `Analyze (actions)` and `Analyze (python)` to succeed on `$hardhatHead`. Re-query rather than inferring readiness from mergeability.

- [ ] **Step 5: Stop for separate merge approval**

Present the PR URL, literal exact head SHA, review state and terminal checks. State that merge approval applies only to that literal SHA. Earlier push approval does not authorise merge.

- [ ] **Step 6: After approval, merge with an exact-head guard and verify merged `main`**

```powershell
$approvedHardhatHead = Read-Host "Enter the exact Hardhat PR head SHA approved for merge"
if ($approvedHardhatHead -cnotmatch '^[0-9a-f]{40}$') { throw "approved Hardhat merge head is not full lower-case hex" }
$hardhatHead = git rev-parse HEAD
if ($hardhatHead -cne $approvedHardhatHead) { throw "local Hardhat HEAD differs from the approved merge SHA" }
$hardhatPr = gh pr view --repo ryanduguid/hardhat-ledger --json number --jq '.number'
$currentHardhatPrHead = gh pr view $hardhatPr --repo ryanduguid/hardhat-ledger --json headRefOid --jq '.headRefOid'
if ($currentHardhatPrHead -cne $approvedHardhatHead) { throw "Hardhat PR head no longer matches approved merge SHA" }
gh pr merge $hardhatPr --repo ryanduguid/hardhat-ledger --squash --match-head-commit $approvedHardhatHead
$hardhatSha = gh pr view $hardhatPr --repo ryanduguid/hardhat-ledger --json mergeCommit --jq '.mergeCommit.oid'
if ($hardhatSha -cnotmatch '^[0-9a-f]{40}$') { throw "merged Hardhat SHA is not full lower-case hex" }
$hardhatMain = gh api repos/ryanduguid/hardhat-ledger/commits/main --jq '.sha'
if ($hardhatMain -cne $hardhatSha) { throw "Hardhat main moved before post-merge acceptance" }
```

Wait for the same four checks on exact `$hardhatSha`. Re-run the frozen-content comparison against the merged tree before proceeding.

---

## Task 12: Re-read release invariants and stop for tag approval

**Files:**

- Read-only: GitHub settings, rules, refs, releases and exact check runs
- Remote mutation after approval only: annotated v0.1.5 tag push

- [ ] **Step 1: Re-read the v0.1.4 evidence and repository controls**

```powershell
$v014TagObject = gh api repos/ryanduguid/hardhat-ledger/git/ref/tags/v0.1.4 --jq '.object.sha'
if ($v014TagObject -cne "84c19467032d37c3291f18c4a7754016abda4bdd") { throw "v0.1.4 tag object changed" }
$v014Commit = gh api repos/ryanduguid/hardhat-ledger/git/tags/$v014TagObject --jq '.object.sha'
if ($v014Commit -cne "2f29bb51957888b1f427be44a7a0866ed4f4f5e5") { throw "v0.1.4 peeled commit changed" }
gh api -H "X-GitHub-Api-Version: 2026-03-10" repos/ryanduguid/hardhat-ledger/immutable-releases --jq '{enabled,enforced_by_owner}'
gh api repos/ryanduguid/hardhat-ledger/rulesets --paginate
```

Require immutable releases enabled and the active `Protect version tags` ruleset still covering `refs/tags/v*`, blocking update and deletion with no bypass actors.

Confirm the v0.1.4 release endpoint is still HTTP 404:

```powershell
$PSNativeCommandUseErrorActionPreference = $false
$v014ReleaseError = gh api repos/ryanduguid/hardhat-ledger/releases/tags/v0.1.4 2>&1
$v014ReleaseExit = $LASTEXITCODE
$PSNativeCommandUseErrorActionPreference = $true
if ($v014ReleaseExit -eq 0 -or "$v014ReleaseError" -notmatch 'HTTP 404') {
    throw "v0.1.4 release absence was not proved"
}
```

A successful response or a non-404 error is a stop condition.

- [ ] **Step 2: Prove v0.1.5 is unused and exact merged `main` is ready**

```powershell
$hardhatPrData = @(gh pr list --repo ryanduguid/hardhat-ledger --state merged --head fix/v0.1.5-release-recovery --limit 2 --json number,mergeCommit | ConvertFrom-Json)
if ($hardhatPrData.Count -ne 1) { throw "could not identify exactly one merged Hardhat recovery PR" }
$hardhatSha = $hardhatPrData[0].mergeCommit.oid
$policyPrData = @(gh pr list --repo ryanduguid/release-policy --state merged --head feat/skill-pack-release-recovery --limit 2 --json mergeCommit | ConvertFrom-Json)
if ($policyPrData.Count -ne 1) { throw "could not identify exactly one merged policy recovery PR" }
$policySha = $policyPrData[0].mergeCommit.oid
git fetch origin main --tags
$apiMain = gh api repos/ryanduguid/hardhat-ledger/git/ref/heads/main --jq '.object.sha'
if ($apiMain -cne $hardhatSha) { throw "GitHub main moved after acceptance" }
gh api repos/ryanduguid/hardhat-ledger/commits/$hardhatSha/check-runs --jq '.check_runs[] | [.name,.status,.conclusion] | @tsv'
$v015RemoteTag = git ls-remote --tags origin refs/tags/v0.1.5
if ($v015RemoteTag) { throw "v0.1.5 tag already exists" }
$releaseWorkflow = git show "$($hardhatSha):.github/workflows/release.yml"
$verifyWorkflow = git show "$($hardhatSha):.github/workflows/verify.yml"
$releasePin = [regex]::Match(($releaseWorkflow -join "`n"), 'release-skills\.yml@([0-9a-f]{40})').Groups[1].Value
$verifyPin = [regex]::Match(($verifyWorkflow -join "`n"), 'verify-skills\.yml@([0-9a-f]{40})').Groups[1].Value
if ($releasePin -cne $policySha -or $verifyPin -cne $policySha) { throw "merged workflow policy pins do not match the approved policy merge" }
```

Confirm the v0.1.5 release endpoint is HTTP 404:

```powershell
$PSNativeCommandUseErrorActionPreference = $false
$v015ReleaseError = gh api repos/ryanduguid/hardhat-ledger/releases/tags/v0.1.5 2>&1
$v015ReleaseExit = $LASTEXITCODE
$PSNativeCommandUseErrorActionPreference = $true
if ($v015ReleaseExit -eq 0 -or "$v015ReleaseError" -notmatch 'HTTP 404') {
    throw "v0.1.5 release absence was not proved"
}
```

Require the four exact checks successful on `$hardhatSha`, and confirm the two workflow pins equal `$policySha`.

- [ ] **Step 3: Stop for explicit annotated-tag approval**

Present the literal `$hardhatSha`, `$policySha`, v0.1.4 tag object and peeled commit, both 404 results, immutable-release setting, tag ruleset, exact checks and the proposed command. State that tag approval applies only to that literal Hardhat SHA. Do not create even the local tag before approval.

- [ ] **Step 4: After approval, create and push only annotated v0.1.5**

```powershell
$approvedHardhatSha = Read-Host "Enter the exact merged Hardhat SHA approved for the v0.1.5 tag"
if ($approvedHardhatSha -cnotmatch '^[0-9a-f]{40}$') { throw "approved tag target is not full lower-case hex" }
$hardhatPrData = @(gh pr list --repo ryanduguid/hardhat-ledger --state merged --head fix/v0.1.5-release-recovery --limit 2 --json mergeCommit | ConvertFrom-Json)
if ($hardhatPrData.Count -ne 1) { throw "could not identify exactly one merged Hardhat recovery PR" }
$hardhatSha = $hardhatPrData[0].mergeCommit.oid
if ($hardhatSha -cne $approvedHardhatSha) { throw "merged Hardhat SHA differs from approved tag target" }
git fetch origin main --tags
if ((git rev-parse origin/main) -cne $approvedHardhatSha) { throw "origin/main moved before tagging" }
git tag -a v0.1.5 $approvedHardhatSha -m "v0.1.5"
if ((git cat-file -t refs/tags/v0.1.5) -ne "tag") { throw "v0.1.5 is not annotated" }
if ((git rev-parse 'refs/tags/v0.1.5^{commit}') -cne $approvedHardhatSha) { throw "v0.1.5 target mismatch" }
git push origin refs/tags/v0.1.5
```

Monitor the release workflow to terminal status. Require guard, read-only verifier and fresh publication core to succeed. If it fails, do not rerun, edit the tag, create a release or upload an asset. Preserve the candidate or draft and return for a human decision.

---

## Task 13: Verify the published v0.1.5 release and close the recovery

**Files:**

- Read-only: published release, assets, attestations, tag and repository state
- Create and remove: one validated temporary download directory

- [ ] **Step 1: Verify release metadata and exact assets**

```powershell
$hardhatPrData = @(gh pr list --repo ryanduguid/hardhat-ledger --state merged --head fix/v0.1.5-release-recovery --limit 2 --json mergeCommit | ConvertFrom-Json)
if ($hardhatPrData.Count -ne 1) { throw "could not identify exactly one merged Hardhat recovery PR" }
$approvedHardhatSha = $hardhatPrData[0].mergeCommit.oid
$tagRef = gh api repos/ryanduguid/hardhat-ledger/git/ref/tags/v0.1.5 | ConvertFrom-Json
if ($tagRef.object.type -ne "tag" -or $tagRef.object.sha -cnotmatch '^[0-9a-f]{40}$') { throw "remote v0.1.5 is not an annotated tag object" }
$v015TagObject = $tagRef.object.sha
$tagObject = gh api repos/ryanduguid/hardhat-ledger/git/tags/$v015TagObject | ConvertFrom-Json
if ($tagObject.object.type -ne "commit") { throw "v0.1.5 annotated tag does not peel to a commit" }
$hardhatSha = $tagObject.object.sha
if ($hardhatSha -cne $approvedHardhatSha) { throw "remote v0.1.5 does not target the approved Hardhat merge" }
$remoteMain = gh api repos/ryanduguid/hardhat-ledger/git/ref/heads/main --jq '.object.sha'
if ($remoteMain -cne $approvedHardhatSha) { throw "remote main no longer matches the approved release commit" }
git fetch origin main --tags
if ((git rev-parse 'refs/tags/v0.1.5^{commit}') -cne $hardhatSha) { throw "local fetched tag does not match the remote tag object" }
$release = gh release view v0.1.5 --repo ryanduguid/hardhat-ledger --json tagName,name,isDraft,isPrerelease,isImmutable,body,assets | ConvertFrom-Json
if ($release.tagName -cne "v0.1.5" -or $release.name -cne "v0.1.5") { throw "release identity mismatch" }
if ($release.isDraft -or $release.isPrerelease -or -not $release.isImmutable) { throw "release state mismatch" }
$listedRelease = @(gh release list --repo ryanduguid/hardhat-ledger --limit 100 --json tagName,isLatest | ConvertFrom-Json) | Where-Object { $_.tagName -ceq "v0.1.5" }
if ($listedRelease.Count -ne 1 -or -not $listedRelease[0].isLatest) { throw "v0.1.5 is not the unique latest release" }
$expectedAssets = @(
    "SHA256SUMS",
    "subcontractor-accounting-skills-0.1.5.spdx.json",
    "subcontractor-accounting-skills-0.1.5.tar.gz",
    "subcontractor-accounting-skills-0.1.5.zip"
) | Sort-Object
$actualAssets = @($release.assets.name) | Sort-Object
if (Compare-Object $expectedAssets $actualAssets -CaseSensitive) { throw "release asset inventory mismatch" }
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = "git"
$start.UseShellExecute = $false
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
[void] $start.ArgumentList.Add("show")
[void] $start.ArgumentList.Add("$($hardhatSha):RELEASE_NOTES.md")
$process = [Diagnostics.Process]::Start($start)
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$taggedNotes = $stdoutTask.GetAwaiter().GetResult()
$gitError = $stderrTask.GetAwaiter().GetResult()
if ($process.ExitCode -ne 0) { throw "could not read tagged release notes: $gitError" }
$normalisedPublishedNotes = ([string] $release.body).Replace("`r`n", "`n")
$normalisedTaggedNotes = $taggedNotes.Replace("`r`n", "`n")
if ($normalisedPublishedNotes -cne $normalisedTaggedNotes) { throw "release notes mismatch" }
```

- [ ] **Step 2: Download and verify local, declared and server digests**

```powershell
$v015TagObject = gh api repos/ryanduguid/hardhat-ledger/git/ref/tags/v0.1.5 --jq '.object.sha'
$hardhatSha = gh api repos/ryanduguid/hardhat-ledger/git/tags/$v015TagObject --jq '.object.sha'
$downloadRoot = Join-Path ([IO.Path]::GetTempPath()) "hardhat-release-verify-$hardhatSha"
if (Test-Path -LiteralPath $downloadRoot) { throw "release verification directory already exists" }
New-Item -ItemType Directory -Path $downloadRoot | Out-Null
gh release download v0.1.5 --repo ryanduguid/hardhat-ledger --dir $downloadRoot
$release = gh release view v0.1.5 --repo ryanduguid/hardhat-ledger --json assets | ConvertFrom-Json
$expectedAssets = @(
    "subcontractor-accounting-skills-0.1.5.spdx.json",
    "subcontractor-accounting-skills-0.1.5.tar.gz",
    "subcontractor-accounting-skills-0.1.5.zip"
)
$checksumLines = Get-Content -LiteralPath (Join-Path $downloadRoot "SHA256SUMS")
$declaredDigests = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
foreach ($line in $checksumLines) {
    if ($line -cnotmatch '^([0-9a-f]{64})  (.+)$') { throw "malformed SHA256SUMS entry" }
    $assetName = $Matches[2]
    if ($assetName -cnotin $expectedAssets) { throw "unexpected path in SHA256SUMS: $assetName" }
    if ($declaredDigests.ContainsKey($assetName)) { throw "duplicate SHA256SUMS entry: $assetName" }
    $declaredDigests[$assetName] = $Matches[1]
}
if ($declaredDigests.Count -ne $expectedAssets.Count) { throw "SHA256SUMS does not contain the exact three payload assets" }
if (Compare-Object ($expectedAssets | Sort-Object) (@($declaredDigests.Keys) | Sort-Object) -CaseSensitive) { throw "SHA256SUMS inventory mismatch" }
foreach ($assetName in $expectedAssets) {
    $path = Join-Path $downloadRoot $assetName
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -cne $declaredDigests[$assetName]) { throw "checksum mismatch for $assetName" }
}
$allAssets = @("SHA256SUMS") + $expectedAssets
$serverDigests = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
foreach ($asset in $release.assets) { $serverDigests[$asset.name] = $asset.digest }
if ($serverDigests.Count -ne $allAssets.Count) { throw "server digest inventory mismatch" }
foreach ($assetName in $allAssets) {
    $local = (Get-FileHash -LiteralPath (Join-Path $downloadRoot $assetName) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($serverDigests[$assetName] -cne "sha256:$local") { throw "server digest mismatch for $assetName" }
}
```

- [ ] **Step 3: Verify all attestations against exact identities**

Run all four provenance checks and both archive SPDX checks in one fresh shell:

```powershell
$v015TagObject = gh api repos/ryanduguid/hardhat-ledger/git/ref/tags/v0.1.5 --jq '.object.sha'
$hardhatSha = gh api repos/ryanduguid/hardhat-ledger/git/tags/$v015TagObject --jq '.object.sha'
git fetch origin main --tags
$releaseWorkflow = git show "$($hardhatSha):.github/workflows/release.yml"
$policySha = [regex]::Match(($releaseWorkflow -join "`n"), 'release-skills\.yml@([0-9a-f]{40})').Groups[1].Value
if ($policySha -cnotmatch '^[0-9a-f]{40}$') { throw "tagged policy pin is invalid" }
$downloadRoot = Join-Path ([IO.Path]::GetTempPath()) "hardhat-release-verify-$hardhatSha"
$assets = @(
    "SHA256SUMS",
    "subcontractor-accounting-skills-0.1.5.spdx.json",
    "subcontractor-accounting-skills-0.1.5.tar.gz",
    "subcontractor-accounting-skills-0.1.5.zip"
)
foreach ($assetName in $assets) {
    gh attestation verify (Join-Path $downloadRoot $assetName) --repo ryanduguid/hardhat-ledger --source-digest $hardhatSha --source-ref refs/tags/v0.1.5 --signer-workflow ryanduguid/release-policy/.github/workflows/publish-archives.yml --signer-digest $policySha
    if ($LASTEXITCODE -ne 0) { throw "provenance verification failed for $assetName" }
}
foreach ($archiveName in @("subcontractor-accounting-skills-0.1.5.zip", "subcontractor-accounting-skills-0.1.5.tar.gz")) {
    gh attestation verify (Join-Path $downloadRoot $archiveName) --repo ryanduguid/hardhat-ledger --source-digest $hardhatSha --source-ref refs/tags/v0.1.5 --signer-workflow ryanduguid/release-policy/.github/workflows/publish-archives.yml --signer-digest $policySha --predicate-type https://spdx.dev/Document/v2.3
    if ($LASTEXITCODE -ne 0) { throw "SPDX verification failed for $archiveName" }
}
```

Require all six commands to exit zero.

- [ ] **Step 4: Verify release and per-asset integrity**

```powershell
$v015TagObject = gh api repos/ryanduguid/hardhat-ledger/git/ref/tags/v0.1.5 --jq '.object.sha'
$hardhatSha = gh api repos/ryanduguid/hardhat-ledger/git/tags/$v015TagObject --jq '.object.sha'
$downloadRoot = Join-Path ([IO.Path]::GetTempPath()) "hardhat-release-verify-$hardhatSha"
$expectedAssets = @(
    "SHA256SUMS",
    "subcontractor-accounting-skills-0.1.5.spdx.json",
    "subcontractor-accounting-skills-0.1.5.tar.gz",
    "subcontractor-accounting-skills-0.1.5.zip"
)
gh release verify v0.1.5 --repo ryanduguid/hardhat-ledger
if ($LASTEXITCODE -ne 0) { throw "release verification failed" }
foreach ($assetName in $expectedAssets) {
    gh release verify-asset v0.1.5 (Join-Path $downloadRoot $assetName) --repo ryanduguid/hardhat-ledger
    if ($LASTEXITCODE -ne 0) { throw "release asset verification failed for $assetName" }
}
```

- [ ] **Step 5: Re-prove v0.1.4 and remove only the validated download root**

```powershell
$v014TagObject = gh api repos/ryanduguid/hardhat-ledger/git/ref/tags/v0.1.4 --jq '.object.sha'
if ($v014TagObject -cne "84c19467032d37c3291f18c4a7754016abda4bdd") { throw "v0.1.4 tag object changed" }
$v014Commit = gh api repos/ryanduguid/hardhat-ledger/git/tags/$v014TagObject --jq '.object.sha'
if ($v014Commit -cne "2f29bb51957888b1f427be44a7a0866ed4f4f5e5") { throw "v0.1.4 peeled commit changed" }
$PSNativeCommandUseErrorActionPreference = $false
$v014ReleaseError = gh api repos/ryanduguid/hardhat-ledger/releases/tags/v0.1.4 2>&1
$v014ReleaseExit = $LASTEXITCODE
$PSNativeCommandUseErrorActionPreference = $true
if ($v014ReleaseExit -eq 0 -or "$v014ReleaseError" -notmatch 'HTTP 404') {
    throw "v0.1.4 release absence was not proved"
}

$v015TagObject = gh api repos/ryanduguid/hardhat-ledger/git/ref/tags/v0.1.5 --jq '.object.sha'
$hardhatSha = gh api repos/ryanduguid/hardhat-ledger/git/tags/$v015TagObject --jq '.object.sha'
$downloadRoot = Join-Path ([IO.Path]::GetTempPath()) "hardhat-release-verify-$hardhatSha"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$fullRoot = [IO.Path]::GetFullPath($downloadRoot)
$relativeRoot = [IO.Path]::GetRelativePath($tempBase, $fullRoot)
$parentPrefix = "..$([IO.Path]::DirectorySeparatorChar)"
$leaf = [IO.Path]::GetFileName($fullRoot)
$unexpectedDownloadRoot = ($relativeRoot -eq ".") -or ($relativeRoot -eq "..") -or [IO.Path]::IsPathRooted($relativeRoot) -or $relativeRoot.StartsWith($parentPrefix, [StringComparison]::OrdinalIgnoreCase) -or ($leaf -cnotmatch '^hardhat-release-verify-[0-9a-f]{40}$')
if ($unexpectedDownloadRoot) {
    throw "refusing to remove an unexpected release verification path"
}
Remove-Item -LiteralPath $fullRoot -Recurse -Force
```

- [ ] **Step 6: Record completion evidence**

Report:

- merged policy SHA and merged Hardhat SHA;
- exact v0.1.5 tag object and peeled commit;
- release workflow run and terminal job results;
- exact four assets and verified local, checksum and server digests;
- four provenance and two SPDX attestation results with the core signer;
- immutable/latest, release and per-asset verification results;
- unchanged v0.1.4 invariant;
- the fact that no skill or accounting content changed.

If any acceptance check fails after publication, do not alter the immutable release. Preserve the evidence and request a new human decision.
