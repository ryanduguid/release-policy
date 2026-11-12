## Policy guarantees

- Fail closed: canonical semver tag, annotated tag object, tag commit equal
  to `main` via the GitHub API when the run starts, clean tree, matching notes
  header, and no existing release for the tag, all verified before any build.
- The first job of a run requires the tag commit to be `main`'s head, before
  the mandatory-checks wait, so the run uses `main`'s current caller workflow,
  policy pin and mandatory checks. Every later gate, including the re-check
  immediately before publication, accepts the head or an ancestor of it when
  GitHub's comparison of the two trees shows no change to the caller workflow
  or to any file under the component's source directory, counting both sides
  of a rename. A merge to `main` that leaves both untouched therefore no longer
  stops a release in progress; one that changes the component, or the caller's
  policy pin, inputs or mandatory checks, still does. The comparison is of the
  trees, not of each commit between them, so a change followed by its exact
  revert passes. For a root release (`.`) any changed file counts. A
  comparison listing 300 or more files may be truncated and fails, as do a
  malformed file record, a rename without its source and a `main` reference
  that is not a full commit SHA. A caller of `publish-archives.yml` directly
  skips the first job's head check but not the later gates.
- The consumer's named mandatory checks have each concluded `success` for the
  exact release commit, in a `push` or `workflow_dispatch` run of `main` in the
  consumer repository, in every run of the named workflow file rather than in
  one chosen by recency. A missing, skipped, cancelled, failed or still-pending
  check, a success for another commit, or a check reported only by a
  pull-request run or a different workflow file stops the release before the
  consumer's tests. A failure in any run refuses even when another run of the
  same workflow passed, which is what a re-run of an earlier run produces. The
  wait for a pending check is bounded at 10 minutes and ends in failure. A
  named check that matches more than one job in a run is refused as ambiguous,
  because a display name is not an identifier and choosing between the matched
  jobs would let a failed job hide behind a passing namesake.
- Consumer tests, wheel and sdist build run without repository write, OIDC or
  attestation authority. Publication receives only the immutable candidate
  artefact ID produced by that job.
- Exact wheel, sdist and SPDX SBOM inventory, canonical release manifest,
  verified `SHA256SUMS`, Actions artefact API digest and run/source binding.
- Provenance attestation on every release asset and an SBOM attestation on the
  wheel, verified against source commit, tag ref, signer workflow and policy
  commit before release creation.
- Both families publish the signed provenance bundle as one more release asset,
  `<stem>-<version>.intoto.jsonl`: one Sigstore bundle on one JSON line, which
  OpenSSF Scorecard's Signed-Releases check reads as release provenance. Every
  provenance subject is verified against the bundle before release creation.
  `SHA256SUMS` and `release-manifest.json` do not list it, because the
  attestation covers those files and is created after them. To verify a
  downloaded asset against the bundle instead of GitHub's attestation API:

      gh attestation verify <file> --bundle <stem>-<version>.intoto.jsonl \
        --repo <owner>/<repo> --signer-repo ryanduguid/release-policy

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
- Once a release is published and verified, the run's job summary lists each
  asset with its SHA-256 digest and the `gh attestation verify` command,
  pinned to the family's signer workflow, the policy commit, the tag and the
  source commit. If the digest inventory cannot be read or validated, or the
  summary cannot be written, the run logs a warning instead of a complete
  summary and the release still stands.
- Consumers pin this repository by full commit SHA and upgrade by reviewed
  pull request (ADR-0001).

Phase 2 and phase 3 designs are recorded in
[ADR-0001](adr/0001-shared-release-policy.md). Skill packs use a
separate adapter so their inventory and stronger validation controls are not
reduced to the source-archive contract. The historical notes-only `v0.1.0`
marker proves neither release phase.

## SLSA Build Level

The release workflows are designed to support
[SLSA v1.0](https://slsa.dev/spec/v1.0/requirements) Build Level 3. This
repository does not state that any release reaches that level: the design has
not been audited against every requirement, and nobody has assessed it
independently. SLSA now marks v1.0 as retired and v1.2 as current; GitHub's
statement below is about v1.0. A consumer's 'SLSA 3' badge adds no assurance to
this page.

The design rests on these points:

- GitHub says [artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
  alone meet SLSA v1.0 Build Level 2, and that a reusable workflow can isolate
  the build from the calling workflow to meet Build Level 3.
- Every release family builds and signs inside this repository's reusable
  workflows, pinned by commit. A caller sets their inputs but not their steps.
- No job that runs consumer code holds `id-token: write` or
  `attestations: write`. The Python and binary families build in one job and
  attest in another; the archive job, which skill packs share, attests but runs
  no consumer code.

These points show where permissions sit and how jobs are separated. They leave
unverified:

- Whether GitHub-hosted runners meet the Hosted and Isolated requirements,
  including that runs cannot influence one another. This repository cannot test
  that.
- Whether each family's real attestations meet the Unforgeable requirement. No
  audit has inspected them against it.

The same account maintains this repository and its consumers, so the reusable
workflow isolates a build from its caller, not from that maintainer. A build
level says how an artefact was built, not that its source was reviewed or is
correct.

Verification is a separate step and does not change a build level. To check that
the expected workflow signed an asset, use `--signer-workflow`, as
[Verify a release](verify-a-release.md) shows. `--signer-repo` accepts any
workflow in this repository, so it does not establish that.

## Release-family canaries

[`canaries.json`](../canaries.json) names one active Python, archive, skill and
verification consumer. Each entry binds the consumer's current literal policy
pin to the latest successful production-shaped run and the reusable workflow
SHA GitHub recorded for that run. `python scripts/check_canaries.py` validates
the manifest offline; the scheduled CI run adds `--live --require-current-evidence` to detect pin drift,
release evidence that has been overtaken, and a pin that no branch or tag of
this repository reaches any more. GitHub refuses a reusable-workflow call at
such a commit before any job starts, so after a history rewrite here a
consumer's pin can match its workflow byte for byte and still fail every
release. The audit asks GitHub's compare API how the pin sits relative to
`main` and accepts `identical` or `behind`. A pin off `main` passes only when
a version tag here, locked by an active ruleset against update and deletion,
reaches it and a commit on `main` still carries the pin's exact tree; the
audit then names that commit as the repin target. A history rewrite therefore
leaves released pins working without loosening the evidence checks, while any
other pin off `main` still fails.

The freshness rule differs by family, because the consumers differ. An
archive, binary, Python or skills release runs on a tag, so a success newer than the
recorded evidence means a release happened that nobody wrote down, and the
audit fails until a person records it. The verification consumer runs on every
push to its default branch, so a newer success is the ordinary state; the
audit instead requires the newest success to be running the pin the consumer
declares today. The recorded run stays the reviewed example either way, and is
checked against GitHub the same way for every family.

Each live audit also requires an active tag ruleset blocking update and deletion
of each release family's version tags, and fails if it cannot read the rulesets.
Bypass actors are checked only when the token can see them; otherwise the audit
reports a notice that they were not checked.

Each live audit also verifies the recorded release's signed SLSA provenance for
the 4 release families. It reads the release by tag and requires it to be
published and immutable, requires the tag to resolve to the recorded commit,
downloads the release's `.tar.gz` asset (for a binary release, its
`SHA256SUMS`) and compares its SHA-256 with the digest GitHub reports, then runs
`gh attestation verify` with the signer workflow and the recorded policy pin as
`--signer-digest`, the tag as `--source-ref` and the commit as
`--source-digest`. The verified certificate must name exactly the recorded run.
The archive and skills releases are signed by `publish-archives.yml`, binary
releases by `release-binary.yml` and Python releases by `release-python.yml`.
The same audit reads the published releases and requires the newest release in
the family's tag namespace, by publication time, to be the recorded tag,
because a release can be published without a successful run to show for it.
A scheduled job of its own audits each binary canary's executables against a
fresh RustSec advisory database, as the README describes.

From 1 October 2026 GitHub deletes workflow runs after the repository's
retention period, at most 90 days for a public repository, so every recorded
run eventually returns 404. A 404 within 90 days and a day of its recorded start
is a missing run and fails. After that the run has expired, and the audit
reports `INFO` instead of failing:

- for a release family, when the signed provenance above verifies and the
  newest published release in the family's tag namespace, by publication time,
  is still the recorded tag. The run's start time and conclusion can no longer
  be checked; the provenance establishes the signed release, not the run record.
- for the verification family, when the newest success on `main` is a later run
  whose own record confirms its ID, success, branch, workflow, later start time
  and the current pin.

A newer release still fails as `NEWER_RELEASE_REVIEW_REQUIRED` until a person
records it, whether or not the old run has expired. Expiry alone never requires
a manifest change. A present-day release listing cannot show a release that was
published and later deleted.

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

A release family's current pin may be newer than its last authorised release.
The scheduled audit fails while that pin lacks release evidence. Record the next
authorised release before claiming the current pin is verified. An offline
manifest check validates structure only; it does not clear that failure or
authorise a release to manufacture evidence.

## Baseline scope

The digest baseline detects changes to its listed documents. The repository
owner can change both a document and its expected digest. It does not protect
against that owner, and does not cover scripts or `canaries.json`. Script tests,
branch coverage, manifest validation and live canary checks cover separate
contracts. None is proof of a consumer release unless its run is recorded.
