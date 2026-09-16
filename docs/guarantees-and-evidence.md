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
  published release is never rolled back by mutation. Immutable state,
  notes and asset digests are rechecked after publication.
- The source-archive family preserves the exact candidate artefacts, verifies
  provenance and both archive SBOM attestations before publication, rechecks
  remote tag/main/release absence, binds inspection to the draft create URL and
  numeric release ID, and verifies immutable release state afterwards.
- Publication requests the repository-wide latest label, but that label is
  not a verification gate. Another component may become latest while the
  exact published release and its assets are being verified.
- Consumers pin this repository by full commit SHA and upgrade by reviewed
  pull request (ADR-0001).

Phase 2 and phase 3 designs are recorded in
[ADR-0001](adr/0001-shared-release-policy.md). Skill packs use a
separate adapter so their inventory and stronger validation controls are not
reduced to the source-archive contract. The historical notes-only `v0.1.0`
marker proves neither release phase.

## Release-family canaries

[`canaries.json`](../canaries.json) names one active Python, archive, skill and
verification consumer. Each entry binds the consumer's current literal policy
pin to the latest successful production-shaped run and the reusable workflow
SHA GitHub recorded for that run. `python scripts/check_canaries.py` validates
the manifest offline; the scheduled CI run adds `--live` to detect pin drift,
release evidence that has been overtaken, and a pin that no branch or tag of
this repository reaches any more. GitHub refuses a reusable-workflow call at
such a commit before any job starts, so after a history rewrite here a
consumer's pin can match its workflow byte for byte and still fail every
release. The audit asks GitHub's compare API how the pin sits relative to
`main` and accepts only `identical` or `behind`.

The freshness rule differs by family, because the consumers differ. An
archive, Python or skills release runs on a tag, so a success newer than the
recorded evidence means a release happened that nobody wrote down, and the
audit fails until a person records it. The verification consumer runs on every
push to its default branch, so a newer success is the ordinary state; the
audit instead requires the newest success to be running the pin the consumer
declares today. The recorded run stays the reviewed example either way, and is
checked against GitHub the same way for every family.

Archive and Python entries can opt into a component namespace with the optional
`tag_prefix` field, a lower-case alphanumeric name separated by single hyphens.
Recorded evidence and latest-success selection must both match exactly
`<tag_prefix>/vMAJOR.MINOR.PATCH`. An omitted or empty prefix retains root
`vMAJOR.MINOR.PATCH` tags; skills and verification entries cannot use a non-empty
prefix. A sibling component's successful release never satisfies the canary.

The maintained canaries, selected in `canaries.json`, use the Australian Tax
Legislation Corpus archive release, Accounting Review Pipeline's Review Ready
Python release, and Australian Accounting Skills' release and read-only shared
verification workflow. Existing published releases supply the
release evidence; migrating these references does not require new releases.

A release family's current pin can temporarily be newer than its latest
release evidence. The live audit reports that state explicitly, as a warning
rather than a failure, without inventing a privileged dry run. It becomes
current evidence only when that consumer completes its next authorised release
through the pinned workflow. The verification entry carries no such warning:
it publishes nothing to wait for, and its next push already proves or
disproves the current pin.
