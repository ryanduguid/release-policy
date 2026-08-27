# Release-policy module: design

- Date: 2026-08-16
- Status: approved 2026-08-16
- Governing decision: [ADR-0001](../../adr/0001-shared-release-policy.md)

> Update, 27 August 2026: the arbitrary `version-command` described in this
> historical phase-1 design is superseded by the closed `pyproject` and
> `python-literal` parsers in
> [the boundary-hardening design](2026-08-27-python-release-boundary-hardening-design.md).

## Problem

Release policy is implemented ten times across the account at three maturity levels. Evidence at current heads:

| Family | Repositories | State |
|---|---|---|
| Packaged Python | au-tax-change-impact-monitor, monthly-close-control-plane, xero-ai-review-gateway, plus one more to confirm at migration | 100-line `release.yml` each; only the wheel filename stem and version command differ |
| Source archive | accounting-excel-toolkit plus three | 110-line `release.yml`; 88-line `build_release_archives.py` byte-identical in four repos, its test in three, draft-release lookup in two |
| Skill packs | australian-accounting-skills, subcontractor-accounting-skills | 309-line hardened variant: semver regex, API main check, dirty-tree gate, paginated no-overwrite |

Every policy fix must land up to ten times or drift. Ten per-repo hardening drafts from 15 August are open against these files; ADR-0001 supersedes them.

## Goals

1. One policy core owning the release gate set, artefact integrity steps and publish lifecycle.
2. Consumers reduced to a thin caller pinned to a full commit SHA.
3. Read-only conformance separated from publish permissions.
4. Gates testable outside a release run.

## Non-goals

- No PyPI publishing (current workflows publish GitHub releases only).
- No tag creation and no inference of publication approval; the human-created annotated tag is the approval act.
- No cross-repository secrets.
- No changes to `.github`.
- DrDebits stays out of scope (different artefact type; possible later adapter).

## Architecture

New repository `ryanduguid/release-policy`:

```
.github/workflows/
  release-python.yml     # reusable (workflow_call): packaged-Python family
  ci.yml                 # module CI: actionlint + shellcheck + gate tests + dry-run fixture build
scripts/
  gates.sh               # release-readiness gates, extracted for testability
tests/                   # bash tests running gates.sh against synthetic git fixtures
fixtures/demo-pkg/       # minimal pyproject package for dry-run determinism builds
docs/adr/0001-shared-release-policy.md
docs/superpowers/specs/  # this document
README.md  RELEASE_NOTES.md  LICENSE
```

The archive-family scripts (`build_release_archives.py`, its test, the draft-release lookup) move into `scripts/` in phase 2, not now.

## Policy core

Canonical gate set, adopted from the hardened skill-pack variant:

1. Tag matches `^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`.
2. Tag is an annotated tag object.
3. Tag commit equals `GITHUB_SHA` and equals `main` as reported by the GitHub API.
4. `RELEASE_NOTES.md` first line equals `# vX.Y.Z` for the tag.
5. Working tree clean (`git status --porcelain --untracked-files=no` empty).
6. No existing release for the tag, checked with pagination; refuse to replace.

Then, in order: locked test suite (`uv run --locked`), build wheel and sdist, SPDX SBOM for the wheel (anchore/sbom-action), `SHA256SUMS` written and immediately verified, provenance attestation over all assets, SBOM attestation for the wheel, `gh release create --verify-tag --draft`, upload assets, flip draft to published latest.

Workflow hygiene carried over: all action pins full-SHA with version comments, `persist-credentials: false`, `fetch-depth: 0`, per-repo concurrency group with `cancel-in-progress: false`, `set -euo pipefail` in every shell step.

### Inputs

Zero family inputs by default. Package name and version both derive from `pyproject.toml`: name normalised to the wheel stem (PEP 427: runs of `[^A-Za-z0-9.]` become `_`), version read via `tomllib`. One optional input, `version-command`, exists as a fallback if a consumer's version is dynamic; the gateway currently reads `xero_ai_review_gateway.version.__version__`, and migration verifies whether its `pyproject.toml` carries a static version before using the fallback.

## Consumer contract

Caller workflow in each consumer, approximately 15 lines:

```yaml
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
    uses: ryanduguid/release-policy/.github/workflows/release-python.yml@<full-40-char-sha>
```

Documented prerequisites in the module README: annotated tags only, `RELEASE_NOTES.md` first-line convention, `uv.lock` committed, `pyproject.toml` with static name and version (or the `version-command` fallback).

## Testing

- `gates.sh` holds every gate so module CI can exercise them without a release: synthetic git fixtures cover wrong tag form, lightweight tag, tag off `main`, missing notes header, dirty tree and pre-existing release.
- Determinism: module CI builds `fixtures/demo-pkg` twice and compares digests.
- Static checks: `actionlint` and `shellcheck`, both pinned.
- Publish paths cannot be exercised by the module itself (it is not a Python package); the module's v0.1.0 release is a notes-only marker created manually. The first pilot release is therefore the first live run of the publish path and is treated as a supervised event; a module self-release path arrives with the phase 2 source-archive workflow.

## Rollout

- **Phase 0:** create the repository, land ADR-0001, this spec, `gates.sh`, tests, `release-python.yml`, module v0.1.0.
- **Phase 1 (pilot):** migrate the three packaged-Python repos; each PR replaces the 100-line workflow with the caller, pins the module SHA and closes that repo's superseded hardening draft.
- **Phase 2:** source-archive family; shared scripts move into the module.
- **Phase 3:** skill packs.

Phases 2 and 3 are named, not designed; each gets its own design pass.

## Failure and recovery

Fail closed at every gate. A defective module version blocks a consumer's next release run only; recovery is repointing the caller's SHA to the previous good module commit. The module never mutates consumer code, tags or published releases.

## Open questions for implementation

1. Resolved 2026-08-17: xero-ai-review-gateway declares `dynamic = ["version"]`, so its pilot caller passes the `version-command` input (PR #20).
2. Open for phase 2 scoping: the fourth packaged-Python repository.
3. Resolved 2026-08-17: superseded drafts were au-tax-change-impact-monitor #18, monthly-close-control-plane #19, xero-ai-review-gateway #16, closed by the pilot PRs (#21, #23, #20 respectively). The pilots pin module commit 5923aba7a8bb680cf4134b8c810c56742d9ec721.
