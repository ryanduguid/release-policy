## Policy guarantees

- Fail closed: canonical semver tag, annotated tag object, tag commit equal
  to `main` via the GitHub API, clean tree, matching notes header, and no
  existing release for the tag, all verified before any build.
- Consumer tests, wheel and sdist build run without repository write, OIDC or
  attestation authority. Publication receives only the immutable candidate
  artefact ID produced by that job.
- Exact wheel, sdist and SPDX SBOM inventory, canonical release manifest,
  verified `SHA256SUMS`, Actions artefact API digest and run/source binding.
- Provenance attestation on every release asset and an SBOM attestation on the
  wheel, verified against source commit, tag ref, signer workflow and policy
  commit before release creation.
- Draft creation and asset upload are bound to the returned numeric release ID.
  A pre-publication failure deletes only that exact current-run draft; a
  published release is never rolled back by mutation. Immutable/latest state,
  notes and asset digests are rechecked after publication.
- The source-archive family preserves the exact candidate artefacts, verifies
  provenance and both archive SBOM attestations before publication, rechecks
  remote tag/main/release absence, binds inspection to the draft create URL and
  numeric release ID, and verifies immutable/latest release state afterwards.
- Consumers pin this repository by full commit SHA and upgrade by reviewed
  pull request (ADR-0001).

Phase 2 and phase 3 designs are recorded in
[ADR-0001](../docs/adr/0001-shared-release-policy.md). Skill packs use a
separate adapter so their inventory and stronger validation controls are not
reduced to the source-archive contract. The historical notes-only `v0.1.0`
marker proves neither release phase.

## Release-family canaries

[`canaries.json`](../canaries.json) names one active Python, archive, skill and
verification consumer. Each entry binds the consumer's current literal policy
pin to the latest successful production-shaped run and the reusable workflow
SHA GitHub recorded for that run. `python scripts/check_canaries.py` validates
the manifest offline; the scheduled CI run adds `--live` to detect pin drift or
newer unrecorded successes.

Archive and Python entries can opt into a component namespace with the optional
`tag_prefix` field, a lower-case alphanumeric name separated by single hyphens.
Recorded evidence and latest-success selection must both match exactly
`<tag_prefix>/vMAJOR.MINOR.PATCH`. An omitted or empty prefix retains root
`vMAJOR.MINOR.PATCH` tags; skills and verification entries cannot use a non-empty
prefix. A sibling component's successful release never satisfies the canary.

The maintained canaries use Accounting Review Pipeline's Xero archive and
Review Ready Python releases, plus Australian Accounting Skills' release and
read-only shared verification workflow. Existing published releases supply the
release evidence; migrating these references does not require new releases.

A current pin can temporarily be newer than the latest release evidence. The
live audit reports that state explicitly without inventing a privileged dry
run. It becomes current evidence only when that consumer completes its next
authorised release through the pinned workflow.
