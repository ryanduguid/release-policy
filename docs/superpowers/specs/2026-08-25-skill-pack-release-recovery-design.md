# Skill-pack release recovery: initial phase 3 adapter design

- Date: 2026-08-25
- Status: approved in chat on 2026-08-25; written specification awaiting review
- Governing decision: [ADR-0001](../../adr/0001-shared-release-policy.md)
- Related designs:
  [source-archive phase 2](./2026-08-21-source-archive-release-design.md)
  and [skill-pack phase 3](./2026-08-21-skill-pack-release-design.md)

## Decision summary

Add a dedicated skill-pack release adapter without widening the source-archive
contract. Both release families will use one nested, privileged publication
workflow that starts from fresh checkouts and owns deterministic archive
creation, attestations, draft inspection and publication.

The initial skill-pack interface supports one allow-listed verification mode,
`subcontractor-accounting-v1`. It runs consumer-controlled dependency
installation and tests only in a separate `contents: read` job. The privileged
publication job receives no files, artefacts, environment state, outputs or
secrets from that job. A successful dependency between jobs is the only signal
that crosses the boundary.

`ryanduguid/hardhat-ledger` will use the new adapter for a reviewed v0.1.5
recovery. The protected annotated v0.1.4 tag remains at
`2f29bb51957888b1f427be44a7a0866ed4f4f5e5`; no v0.1.4 release or assets will
be created.

## Incident and need

Hardhat Ledger's v0.1.4 caller used `release-archive.yml`. That workflow
correctly enforced its documented standard-library test contract and did not
install consumer dependencies. Hardhat Ledger's tests import PyYAML from its
tracked root `requirements-test.txt`, so release run 32839062910 stopped in the
read-only consumer-test job with `ModuleNotFoundError: No module named 'yaml'`.
The privileged job was skipped and GitHub created no release or release assets.

The failure exposed a family mismatch rather than a reason to add an arbitrary
dependency or test input to the source-archive workflow. The phase 3 design
already treats skill packs as a separate family because they require exact
inventory, fabricated-validation and Skills CLI checks.

The policy foundation, archive builder and pre-publication controls are already
implemented and locally tested. The failed run also demonstrated that a
consumer-test failure stops before publication authority is granted. It did not
prove a successful live source-archive publication. Because the protected
v0.1.4 tag cannot be moved or deleted, Ryan approved a narrowly scoped phase 3
adapter and a new v0.1.5 recovery instead of weakening phase 2 or retagging.

## Relationship to the earlier phase 3 design

This specification supersedes only the proposed initial interface and
migration order in the 2026-08-21 skill-pack design:

- The initial adapter has three inputs, not five. It does not expose
  `release-notes-path` or `archive-builder-mode`.
- Root `RELEASE_NOTES.md` and the common policy-owned archive builder are fixed
  for `subcontractor-accounting-v1` because the current Hardhat Ledger tree
  already follows both contracts.
- Hardhat Ledger is the first phase 3 consumer because it has a protected failed
  tag and needs a policy-compliant recovery. This is an explicit exception to
  the former `australian-accounting-skills`-first order.
- Ryan's approval of this recovery explicitly waives the earlier requirement
  for a successful live phase 2 publication before phase 3 implementation. It
  does not declare phase 2 proven. Policy tests must validate the shared
  verifier and workflow contracts; the exact verifier must then pass on the
  Hardhat pull request and merged `main` before the separately authorised,
  supervised v0.1.5 run.
- `australian-accounting-skills` remains unmigrated. Adding it requires a later
  design update supported by its current repository evidence.

All other phase 3 controls remain in force, including a closed interface,
exact skill and validation inventories, candidate preservation, exact assets,
pre-publication attestation checks and post-publication verification.

## Goals

1. Give skill-pack consumers a fixed, dependency-aware verification contract.
2. Keep consumer-controlled code away from release write and OIDC permissions.
3. Remove duplicated privileged publication logic between archive families.
4. Run the exact skill verifier on pull requests and `main`, before a tag can
   trigger publication.
5. Recover Hardhat Ledger with a reviewed v0.1.5 release while preserving the
   failed v0.1.4 tag as immutable evidence.
6. Preserve the existing source-archive inputs, test contract, assets and
   publication behaviour for a consumer that advances its full-SHA policy pin;
   document and test the intentional signer-workflow change caused by the
   nested core.

## Non-goals

- Moving, deleting or recreating v0.1.4.
- Creating a v0.1.4 release, manually uploading its assets or editing its tag.
- Adding arbitrary commands, dependency paths, asset globs, build modes or
  repository-external paths to a reusable workflow.
- Migrating `australian-accounting-skills` in this recovery.
- Changing Hardhat Ledger's ten skills, accounting content, validation cases or
  professional-review boundary.
- Changing the packaged-Python release family.
- Automatically pushing branches, opening or merging pull requests, or creating
  a tag. Each remote action retains its existing human approval gate.

## Architecture and trust boundary

The policy repository gains three reusable workflows and turns the existing
source-archive workflow into a thin family adapter:

```text
Hardhat pull request or main
  verify.yml, contents: read, full policy SHA
    -> verify-skills.yml
         -> fresh consumer checkout
         -> fixed skill-pack verification

Hardhat annotated version tag
  release.yml, full policy SHA
    -> release-skills.yml
         -> verify-skills.yml, contents: read
         -> publish-archives.yml, only after verification succeeds
              attestations: write, contents: write, id-token: write
              fresh consumer and policy checkouts

Source-archive annotated version tag
  existing thin caller, full policy SHA
    -> release-archive.yml
         -> fixed standard-library consumer tests, contents: read
         -> publish-archives.yml, only after tests succeed
```

`release-archive.yml` and `release-skills.yml` own their family-specific
verification. `publish-archives.yml` owns only the common privileged release
lifecycle. It never runs consumer tests because doing so would execute
consumer-controlled code with write and OIDC permissions.

The nested workflows use relative `./.github/workflows/...` references. GitHub
resolves a same-repository relative reusable-workflow reference from the same
commit as its caller. Each executable policy job also requires
`job.workflow_sha` to be 40 hexadecimal characters, checks out
`ryanduguid/release-policy` at that SHA and proves that the checkout's `HEAD`
equals it. GitHub defines `job.workflow_sha` as the commit containing the
workflow file that defines the current job.

Consumer and policy sources are checked out into separate sibling directories,
`consumer/` and `policy/`. No consumer-controlled tracked path can therefore
collide with or shadow the policy checkout. Every Git and consumer command sets
`consumer/` as its working directory; policy programs are invoked from the
sibling checkout.

The chain has no `secrets: inherit`. The verification workflow declares no
outputs and does not upload or cache anything for publication. The publication
job uses `needs` only as a success gate, receives the original scalar inputs,
and starts on a new runner. GitHub token permissions can remain the same or be
reduced through a nested workflow chain; they cannot be elevated above the
permissions granted by the top-level caller.

## Workflow contracts

### `verify-skills.yml`

This reusable workflow is safe to call from pull-request, `main` and tag
workflows. Its complete public interface is:

| Input | Required | Contract |
|---|---:|---|
| `skills-verification-mode` | yes | Closed allow-list; initially only `subcontractor-accounting-v1` |
| `version-file` | no | Defaults to `VERSION`; safe relative tracked regular file |

It declares no secrets and no outputs. The sole job has `contents: read`, a
15-minute timeout, a fresh consumer checkout with credentials not persisted,
the policy checkout bound to `job.workflow_sha`, and Python 3.12.

A policy-owned verifier validates the mode and required paths before running
any consumer command. It invokes subprocesses as argument arrays without a
shell. The workflow neither uploads an artefact nor exposes an output that a
privileged job could consume.

### `release-skills.yml`

This is the only supported release entry point for the initial skill-pack
family:

| Input | Required | Contract |
|---|---:|---|
| `artifact-stem` | yes | Lower-case hyphenated identifier |
| `version-file` | no | Defaults to `VERSION`; safe relative tracked regular file |
| `skills-verification-mode` | yes | Closed allow-list; initially only `subcontractor-accounting-v1` |

It declares no custom secrets and no outputs. A small `contents: read` guard job
rejects an empty or unknown mode. For `subcontractor-accounting-v1`, that guard
exits non-zero for frozen tag `v0.1.0`. Both later jobs depend on the guard, so
the overall called workflow fails rather than succeeding with skipped jobs. It
then calls `verify-skills.yml` at the same policy commit with `contents: read`.
Only a successful verifier permits the adapter to call `publish-archives.yml`
at that same commit with these exact permissions:

```yaml
permissions:
  attestations: write
  contents: write
  id-token: write
```

No file, artefact, environment mutation or command output from verification is
passed to publication.

### `publish-archives.yml`

This nested workflow is the policy's internal common publication core. Its
only inputs are `artifact-stem` and `version-file`, with the same closed
contracts as `release-archive.yml`. It has no test-command, dependency,
release-notes, builder or asset-glob input.

Its single job owns non-cancelling concurrency for the consumer repository and
tag. It starts from fresh consumer and policy checkouts, verifies the full
policy SHA, and performs the existing source-archive lifecycle:

1. Require a canonical semver tag and annotated tag object.
2. Require the tag commit, event SHA and API-reported `main` commit to match.
3. Require a clean tracked tree, a canonical version file matching the tag, a
   root `RELEASE_NOTES.md` headed by that tag, and no existing release.
4. Build deterministic LF/UTC ZIP and tar archives from the tagged commit.
5. Generate an SPDX 2.3 SBOM and require the exact four-asset inventory.
6. Write and immediately verify `SHA256SUMS`.
7. Preserve the exact candidate assets for 14 days under an artefact name that
   includes the tag, workflow run ID and run attempt. Never overwrite a prior
   attempt's candidate artefact.
8. Create provenance attestations for all four assets and SPDX attestations for
   both archives, then verify them before publication.
9. Recheck the remote peeled tag, event SHA, API-reported `main` and release
   absence immediately before creating a draft.
10. Create a draft and bind all later reads and writes to the exact returned URL
    and numeric release ID, including while GitHub reports an `untagged-*`
    draft.
11. Compare the draft's notes, exact asset names and server-reported SHA-256
    digests with the local candidates.
12. Recheck the tag and `main`, publish only that release ID as latest, and
    verify its immutable, non-draft, non-prerelease and tag-bound state.
13. Recompare notes, names and digests, then run release and per-asset
    verification.

Because the attestation action runs in this core, the required signer workflow
becomes
`ryanduguid/release-policy/.github/workflows/publish-archives.yml`. Every
attestation verification also requires the exact policy commit as signer
digest, the consumer tag ref and the consumer commit digest.

The core is reusable because nesting requires `workflow_call`, so GitHub cannot
make it technically private to the two adapters. Direct consumer calls are
unsupported. Policy contract tests require both supported callers to use their
family adapter, and each migrated consumer must test its exact adapter path and
full policy SHA. A reviewed consumer workflow change remains the enforcement
boundary against bypassing its adapter. Existing consumers pinned to older
policy commits remain unchanged.

### `release-archive.yml` compatibility

The source-archive adapter retains its two-input public contract and fixed
command:

```text
python -B -m unittest discover -s tests -v
```

That command remains in a separate `contents: read` job. After it succeeds, the
adapter calls `publish-archives.yml` with the original `artifact-stem` and
`version-file`. The publication steps, assets and failure behaviour remain the
same. Consumers that advance to the new policy commit must expect the core
workflow path as the attestation signer; consumers pinned to an earlier commit
keep their earlier signer identity.

## `subcontractor-accounting-v1` verification mode

The initial mode requires these consumer-owned paths before dependency
installation:

- the supplied `version-file`, initially root `VERSION`;
- root `requirements-test.txt`;
- `scripts/validate_validation.py`;
- `tests/verify_skills_cli.py`; and
- a real `tests` directory containing tracked test files.

Each named file must be inside the checkout, tracked by Git with regular-file
mode `100644` or `100755`, and have no symlink path component. The `tests`
directory must not be a symlink. The version file must contain exactly one
canonical `MAJOR.MINOR.PATCH` line. Missing, untracked, non-regular, symlinked,
outside-root or malformed inputs fail before pip or consumer code runs.

This symlink rule applies to the verification entry points and their path
components. Archive membership remains the tracked Git tree produced by the
common archive builder; this recovery does not add a repository-wide symlink
ban to either release family.

The policy then runs these commands in order and stops on the first failure:

```text
python -m pip install --isolated --disable-pip-version-check --no-input --no-deps --requirement requirements-test.txt
python -B -m unittest discover -s tests -v
python scripts/validate_validation.py
python tests/verify_skills_cli.py
```

The requirements filename, Python version, commands and order are fixed by the
mode. The dependency file remains reviewed consumer source; it cannot be
redirected through an input. `--isolated` ignores user and environment pip
configuration, `--no-input` prevents prompts, and `--no-deps` prevents
unlisted transitive dependency installation.

The consumer's unit tests own its exact manifest and ten-skill inventory. Its
fabricated-validation program owns the exact nine-card inventory, safety checks
and full skill coverage. Its Skills CLI program owns the exact ten-skill CLI
discovery and pins `skills@1.5.22`. The shared policy fixes which programs run;
it does not duplicate those changing inventories in release-policy.

These commands execute reviewed consumer code and can use the network for pip
and the Skills CLI. The containing job has no write or OIDC permission, no
custom secrets, no persisted checkout credential and no publication state.
The publication core never executes these programs.

## Failure and recovery behaviour

Every branch fails closed:

| Failure point | Required result |
|---|---|
| Unknown mode, unsafe path or missing tracked file | Stop before dependency installation |
| `subcontractor-accounting-v1` receives frozen tag v0.1.0 | Fail the guard and the overall called workflow; do not treat skipped dependants as success |
| Dependency installation, unit test, fabricated validation or Skills CLI failure | Fail the read-only job; do not start publication |
| Initial release gate, build, checksum or attestation failure | Fail before draft creation; preserve candidate evidence when available |
| Remote tag, `main` or release state changes before draft creation | Fail without creating a release |
| Draft creation or inspection fails | Leave any created draft untouched; never delete, overwrite or upload replacement files |
| Tag or `main` changes while inspecting the draft | Leave the exact identified draft untouched and fail |
| Publication succeeds but a later verification fails | Leave the published release untouched, report failure and require human inspection |

Short polling loops may read GitHub again to accommodate eventual consistency.
They do not repeat a create, upload or publish mutation. The workflows never
create tags, automatically retry into publication, delete releases or repair
remote state.

A failed immutable version tag advances to a new reviewed version. For this
incident:

- v0.1.4 remains an annotated protected tag at
  `2f29bb51957888b1f427be44a7a0866ed4f4f5e5`;
- a v0.1.4 GitHub release and v0.1.4 assets remain absent;
- no workflow reruns or manual uploads will try to complete v0.1.4; and
- the recovery version is v0.1.5.

## Policy-repository verification

Implementation begins test-first after this specification and its later
implementation plan are approved. Policy tests must cover:

1. The allowed mode and rejection of empty or unknown modes.
2. Frozen v0.1.0 makes the guard and complete called workflow fail, with both
   dependant jobs unable to start.
3. Safe tracked regular files plus missing, untracked, symlinked, non-regular
   and outside-root cases.
4. Canonical and malformed version-file contents.
5. The exact pip and three test commands, argument boundaries and execution
   order without a shell.
6. Immediate short-circuiting after every possible command failure.
7. The absence of arbitrary command, path, glob, notes or builder inputs.
8. `contents: read` on both verification routes and exactly three write
   permissions on the publication route.
9. No secrets inheritance, workflow outputs, cache or artefact transfer from
   verification to publication.
10. Same-commit relative nesting, full-SHA policy checkout and exact core signer
   identity.
11. Separate sibling checkouts that prevent a consumer path from shadowing the
    policy source.
12. Rerun-safe, non-overwriting candidate artefact names containing run ID and
    attempt.
13. The unchanged source-archive input and fixed-test contract plus the
    intentional core signer migration.
14. Existing release gates, deterministic archive generation, exact assets,
    draft-ID binding and failure behaviour.

The repository's required validation remains actionlint, shellcheck, repository
baseline tests, Bash gate tests, Python unit tests, determinism tests and CodeQL
for Python and Actions. Static contract tests must inspect the workflow DAG and
permissions; synthetic Git fixtures must exercise path and mode failures
without publishing.

## Hardhat Ledger recovery change

The recovery is a separate pull request based on exact current remote `main`,
`2f29bb51957888b1f427be44a7a0866ed4f4f5e5`. If `main` changes first, the new
delta must be reviewed before work continues.

The pull request will:

- pin `verify-skills.yml` and `release-skills.yml` to the exact merged
  release-policy commit;
- select `subcontractor-accounting-v1`;
- retain the frozen-v0.1.0 refusal;
- change `VERSION` and both concrete plugin manifests from 0.1.4 to 0.1.5;
- replace the release notes with a v0.1.5 recovery note that records the failed
  v0.1.4 run and states that no skill or accounting content changed;
- update caller contract tests and release procedure text where required; and
- leave all ten `SKILL.md` files, validation cards and accounting rules
  byte-for-byte unchanged.

The local Verify workflow keeps its existing job and adds a second,
full-SHA-pinned shared-conformance job. Both run on pull requests and pushes to
`main`. This makes the exact dependency installation and test path execute
before any release tag exists.

Hardhat acceptance before merge requires:

- all 43 unit tests;
- all nine fabricated validation cards with exact ten-skill coverage;
- exact ten-skill Skills CLI discovery;
- strict Claude marketplace validation;
- isolated Claude and Codex plugin installations reporting version 0.1.5;
- exact manifest, version and release-note agreement;
- a clean `git diff --check` result;
- the local Verify job, shared-conformance job and both CodeQL languages passing
  on the pull request and merged `main`; and
- a remote check that v0.1.4 remains unchanged and has no release.

## Rollout and authority gates

1. Commit and review this design, then approve a separate implementation plan.
2. Implement and independently review the release-policy change.
3. After explicit approval, publish a release-policy pull request. Require CI
   and CodeQL success, inspect the final head and merge only with separate
   approval.
4. Record the exact merged policy commit SHA.
5. Prepare the separate Hardhat Ledger v0.1.5 recovery branch and pull request,
   pinning only that merged SHA.
6. Require all local, pull-request and merged-`main` checks listed above. Merge
   only after explicit approval and an exact-head check.
7. Re-read the immutable-release setting and tag rules, confirm the remote
   `main` SHA, prove the v0.1.4 invariant, and prove no v0.1.5 tag or release
   exists.
8. Present the exact Hardhat commit, policy pin and terminal check results for a
   separate tag-authorisation decision.
9. Only after approval, create annotated tag v0.1.5 at that exact commit and
   monitor the read-only verifier and privileged publication core to terminal
   status.

No approval in an earlier step implies approval to push, merge or tag in a
later step.

## Post-publication acceptance

The v0.1.5 release is accepted only when all of these checks pass:

1. The annotated tag peels to the approved merged Hardhat commit and still
   matches remote `main`.
2. The release is v0.1.5, non-draft, non-prerelease, immutable and latest, with
   notes exactly matching tagged `RELEASE_NOTES.md`.
3. Its asset names are exactly:
   `subcontractor-accounting-skills-0.1.5.zip`,
   `subcontractor-accounting-skills-0.1.5.tar.gz`,
   `subcontractor-accounting-skills-0.1.5.spdx.json` and `SHA256SUMS`.
4. Fresh downloads to a temporary directory match `SHA256SUMS` and the
   server-reported digests.
5. GitHub provenance attestations verify for all four assets against the
   approved Hardhat commit, v0.1.5 tag, core signer workflow and exact policy
   SHA.
6. SPDX attestations verify for both archives against the same identities.
7. GitHub release verification and per-asset verification succeed.
8. The v0.1.4 tag remains at its original commit and a v0.1.4 release remains
   absent.

If GitHub reports publication success but any acceptance check fails, do not
modify the immutable release. Record the discrepancy and require a new human
decision.

## Documentation updates during implementation

The policy README will document the skill-pack caller, verifier-only caller,
closed mode, required files, permission split, core signer identity and the
source-archive signer transition. The earlier phase 3 design will link to this
specification as the approved initial adapter refinement. Hardhat Ledger's
release procedure will document v0.1.5 pre-tag and post-publication checks. The
policy security note and repository-baseline inventory will identify the new
privileged core and complete canonical workflow set.

## Authoritative GitHub behaviour

The design relies on these GitHub-defined properties:

- [Reuse workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows):
  same-repository relative calls use the same commit as the caller.
- [Reusing workflow configurations](https://docs.github.com/en/enterprise-cloud@latest/actions/reference/workflows-and-actions/reusing-workflow-configurations):
  nested workflow token permissions can only stay the same or become more
  restrictive.
- [Contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts):
  `job.workflow_sha`, `job.workflow_repository` and `job.workflow_file_path`
  identify the workflow that defines the current job.
- [Artifact attestations with reusable workflows](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/increase-security-rating):
  attestation verification names the reusable workflow that runs the signing
  action.
