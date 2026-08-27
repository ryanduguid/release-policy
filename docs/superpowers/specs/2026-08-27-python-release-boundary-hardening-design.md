# Python release boundary hardening

- Date: 2026-08-27
- Status: approved through the portfolio remediation architecture
- Governing decision: [ADR-0001](../../adr/0001-shared-release-policy.md)

## Problem

The Python adapter accepted `version-command` and evaluated it through
`bash -c`. The same job then ran consumer tests and builds while holding
repository write and OIDC authority. Its release upload also selected assets
with broad globs and referred to a release by tag after creation. These were
unnecessary execution and identity ambiguities at an irreversible boundary.

## Decision

Keep the Python adapter separate from the source-archive adapters, but deepen
its existing boundary:

1. Package metadata uses one of two closed parsers. `pyproject` reads a static
   `[project].version`. `python-literal` parses a safe relative tracked file and
   accepts exactly one literal string assigned to `__version__`. Neither path
   imports consumer code or invokes a shell.
2. Separate test and build jobs with only `contents: read` use fresh sibling
   `source` and `policy` checkouts. Both repeat the repository gates; the first
   runs locked tests and the second builds and generates the SBOM. The build
   writes a canonical manifest and `SHA256SUMS` for exactly one pure-Python
   wheel, one sdist and one SPDX file.
3. The immutable Actions artefact ID and digest cross to a new publication
   runner. Its consumer source, policy and downloaded candidate remain in three
   sibling directories. Before it uses write authority, the runner verifies the
   artefact's API name, digest, run ID and source SHA, then independently checks
   the manifest, filenames, byte lengths and hashes.
4. The publication runner attests and verifies every exact asset against the
   consumer commit, tag ref, `release-python.yml` signer and pinned policy SHA.
5. Release creation uses the REST response's numeric release ID. Assets upload
   to that ID, draft and published API digests must equal local digests, and the
   tag and `main` are rechecked immediately before publication.
6. An error before publication may delete only that exact numeric draft after
   its tag, name and draft state are re-proven. Once publication is attempted,
   recovery is a new version, never mutation.

## Interface

The reusable workflow accepts:

- `version-parser`: `pyproject` by default, or `python-literal`;
- `version-file`: `pyproject.toml` by default, otherwise a safe tracked path;
- `upload-dist-artifact`: the existing boolean PyPI handoff control.

Callers grant `actions: read`, `attestations: write`, `contents: write` and
`id-token: write` to the reusable job. The workflow confines the three write
capabilities to its publication job.

## Verification

- Metadata tests cover static and dynamic versions, non-literals, duplicates,
  non-canonical versions, traversal, untracked paths, symlinks and shell
  metacharacters without execution.
- Candidate tests cover exact round trips, tampering, extra files, context
  mismatch, control-file overwrite, asset symlinks and directory symlinks.
- Workflow contract tests keep consumer execution in the read-only job and
  require sibling checkouts, an isolated candidate, artefact, attestation,
  draft-ID and post-publication controls.
- Existing gate, archive, skill and determinism suites remain authoritative.

## Consequences

Dynamic-version consumers must replace `version-command` with the closed
parser and file inputs when advancing their policy pin. All Python consumers
must grant `actions: read` so the publisher can inspect the exact Actions
artefact. Historical workflow pins continue to resolve unchanged.
