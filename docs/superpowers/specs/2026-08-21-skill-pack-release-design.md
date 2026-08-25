# Skill-pack release family: phase 3 design

- Date: 2026-08-21
- Status: superseded for the initial interface and migration order by the
  [approved 25 August recovery design](./2026-08-25-skill-pack-release-recovery-design.md)
- Governing decision: [ADR-0001](../../adr/0001-shared-release-policy.md)

The observed-consumer analysis and broader future migration controls remain in
force.

## Why this is a separate family

Skill packs are not ordinary source archives. They have a validated skill
inventory, package-specific release notes and builders, and stronger evidence
requirements before publication. Folding them into the phase 2 interface would
either weaken those controls or expose arbitrary shell inputs.

## Observed consumers

`australian-accounting-skills` uses `VERSION`, `RELEASE_NOTES.md`, the common
archive builder, exact nine-skill validation, candidate artefact preservation,
pre-publication provenance and SBOM verification, exact draft inspection, and
post-publication release and asset verification.

`subcontractor-accounting-skills` uses tag-specific notes, a different builder,
a frozen `v0.1.0` exclusion and a different inventory contract. Those
differences are policy inputs only where both consumers prove a constrained
mode is necessary; otherwise the second repository retains a local adapter.

## Proposed constrained interface

The future workflow may accept `artifact-stem`, `release-notes-path`,
`version-file`, `skills-verification-mode` and `archive-builder-mode`. Every
value must come from a closed allow-list or a safe relative path. It must not
accept arbitrary shell snippets, arbitrary asset globs or repository-external
paths.

The shared implementation must preserve the stronger observed controls:

- exact validated skill inventory and fabricated-fixture validation;
- exact four-asset inventory and candidate preservation;
- provenance verification for every asset before publication;
- SPDX verification for both source archives before publication;
- a remote tag/main/release-absence recheck immediately before draft creation;
- exact create-URL/release-ID binding even while GitHub reports an
  `untagged-*` draft;
- notes, digest and asset comparison before publication; and
- immutable/latest/release/asset verification after publication.

## Migration order

1. Prove phase 2 and its recovery path.
2. Implement the skill-pack adapter with adversarial fixture tests.
3. Pilot `australian-accounting-skills` without reducing any current control.
4. Map `subcontractor-accounting-skills` to the constrained interface or retain
   its local adapter where semantics differ.

The current notes-only `release-policy` `v0.1.0` marker is not evidence that
either phase is distributable. Consumer pins advance one reviewed commit at a
time.
