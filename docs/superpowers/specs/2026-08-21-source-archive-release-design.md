# Source-archive release family: phase 2 design

- Date: 2026-08-21
- Status: approved through the account build plan
- Governing decision: [ADR-0001](../../adr/0001-shared-release-policy.md)

## Scope

Phase 2 adds a reusable workflow for repositories whose release is a
deterministic archive of tracked source. It does not change the packaged-Python
workflow and does not yet migrate a consumer.

The first family consists of `accounting-excel-toolkit`,
`au-tax-legislation-corpus` and `xero-trial-balance-export`.
`australian-accounting-skills` is deliberately excluded: its skill inventory,
candidate preservation and release verification belong to phase 3.

## Caller contract

The workflow accepts only two named inputs:

- `artifact-stem` is required and must be a lower-case hyphenated identifier.
- `version-file` defaults to `VERSION` and must be a safe relative regular-file
  path containing one canonical `MAJOR.MINOR.PATCH` line.

The tag must equal `v` plus that version. Callers cannot supply arbitrary shell
commands, test commands, archive globs or paths outside the checked-out source.
The initial family test contract is `python -B -m unittest discover -s tests
-v`; a consumer that cannot meet it remains on its direct workflow until a
second evidenced case justifies a constrained alternative.

## Build and publication

The workflow reuses the existing policy gates, checks out the policy at
`job.workflow_sha`, verifies that value is a full commit SHA, and runs the
standard test contract. The policy-owned archive builder invokes `git archive`
with explicit LF conversion and UTC environment settings to produce:

1. `<stem>-<version>.zip`
2. `<stem>-<version>.tar.gz`
3. `<stem>-<version>.spdx.json`
4. `SHA256SUMS`

The asset inventory is exact. The workflow verifies local checksums, uploads a
short-lived candidate artefact, creates provenance attestations for every
asset, and attaches the SPDX document to both archives.

Immediately before publication it rechecks the annotated remote tag, remote
`main`, the event commit and release absence. It creates a draft, binds later
inspection to the exact URL or release ID returned by the create operation,
then compares notes, asset names and server-reported digests. Only that release
ID may be published. The published release must be non-draft, non-prerelease,
immutable, latest, tag-bound and byte-verifiable before the job succeeds.

## Failure and recovery

Every gate fails closed. The workflow never creates a tag, overwrites a release
or infers approval. The human-created annotated tag remains the publication
approval act. A failed run leaves candidate artefacts in the workflow run and
may leave an identified draft for a maintainer to inspect; it never deletes or
silently replaces remote state. Recovery is a reviewed policy-pin change or a
new supervised run after the exact failure is understood.

## Migration order

1. Land and review the policy foundation without changing consumers.
2. Pilot `accounting-excel-toolkit` in its own pull request.
3. Migrate `au-tax-legislation-corpus` if its full standard test contract fits.
4. Migrate `xero-trial-balance-export` only after preserving its stronger
   locked-dependency and offline checks in an explicit, reviewable contract.

Each caller pins a reviewed full 40-character policy commit. A consumer removes
its local archive helper only in the same pull request that proves the shared
replacement.
