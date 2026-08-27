# release-policy

Shared release policy for ryanduguid repositories: reusable GitHub Actions
workflows plus testable release-readiness gates. Governed by
[ADR-0001](./docs/adr/0001-shared-release-policy.md); design in
[docs/superpowers/specs/](./docs/superpowers/specs/).

## Release status

`v0.1.0` is a manually created, notes-only historical source marker. It has no
uploaded, checksummed module artefact and is not a verified distributable
module release. It remains mutable, but must not be silently rewritten,
replaced, or retrofitted with assets.

The repository now contains reviewed-in-code source-archive and initial
skill-pack workflow implementations. That does not retrofit `v0.1.0`, make the
module itself distributable, or prove either phase through this repository's
own tag. Each consumer migration remains a separate reviewed change pinned to
a full policy commit.

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
          actions: read
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-python.yml@<full-40-char-commit-sha>

Projects whose `pyproject.toml` declares `dynamic = ["version"]` add:

        with:
          version-parser: python-literal
          version-file: your_package/version.py

The `python-literal` parser reads exactly one top-level literal string assigned
to `__version__`. It parses the tracked file as data and never imports or
executes consumer code. Arbitrary version commands are not supported.

## Publishing the same distribution to PyPI

PyPI cannot name a reusable workflow as a Trusted Publisher
([pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096)), so the
publish step runs in the consumer, not here. This module builds, tests, gates,
SBOMs and attests the distribution, then hands the exact same files to the
caller as a run artifact.

Set the input, then add a second job:

    jobs:
      release:
        permissions:
          actions: read
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-python.yml@<full-40-char-commit-sha>
        with:
          upload-dist-artifact: true

      pypi:
        name: publish to PyPI
        needs: release
        runs-on: ubuntu-latest
        environment:
          name: pypi
          url: https://pypi.org/p/<distribution-name>
        permissions:
          id-token: write # Mint the OIDC token PyPI Trusted Publishing verifies.
        steps:
          - name: Download the attested distribution
            uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
            with:
              name: dist-${{ needs.release.outputs.stem }}-${{ needs.release.outputs.version }}
              path: dist
          - name: Publish to PyPI
            uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2

The `release` job exposes `stem` and `version` outputs so the caller can name
the artifact without repeating the version logic. Consumer tests and the build
run on separate `contents: read` jobs with sibling source and policy checkouts.
The publication job keeps source, policy and candidate data in three sibling
directories, downloads by immutable Actions artefact ID, verifies the API
digest, run and source identity, then verifies the policy-owned candidate
manifest before using write authority.

### One-time PyPI setup, per distribution

Done by a person at pypi.org, not by any workflow:

1. Sign in at [pypi.org](https://pypi.org/) and open **Your account** then
   **Publishing**.
2. Add a **pending** GitHub publisher, which creates the project on first use:
   - PyPI Project Name: the `[project] name` from `pyproject.toml`
   - Owner: `ryanduguid`
   - Repository name: the consumer repository
   - Workflow name: `release.yml` (the caller, never `release-python.yml`)
   - Environment name: `pypi`
3. In the consumer repository, create an environment named `pypi` under
   **Settings** then **Environments**. Add a required reviewer if the publish
   should pause for approval, since a PyPI upload cannot be undone and a
   version number cannot be reused.

The environment name in step 2 and step 3 must match, or the upload is
rejected.

## Using the source-archive release workflow

Source-only callers use the separate family workflow:

    jobs:
      release:
        permissions:
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-archive.yml@<full-40-char-commit-sha>
        with:
          artifact-stem: accounting-excel-toolkit

`version-file` defaults to `VERSION`. The workflow accepts no arbitrary test
command, build command or asset glob. It runs the fixed unittest contract,
builds deterministic ZIP and tar.gz source archives from the tagged commit,
generates an SPDX SBOM, and publishes exactly those three files plus
`SHA256SUMS` after inspecting the exact draft returned by GitHub.

## Using the skill-pack verification and release workflows

Run the shared conformance job on pull requests and `main`:

    name: Verify
    on:
      pull_request:
      push:
        branches: [main]
    permissions:
      contents: read
    jobs:
      shared-conformance:
        permissions:
          contents: read
        uses: ryanduguid/release-policy/.github/workflows/verify-skills.yml@<full-40-char-commit-sha>
        with:
          skills-verification-mode: subcontractor-accounting-v1

Use the separate release adapter for annotated version tags:

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
        uses: ryanduguid/release-policy/.github/workflows/release-skills.yml@<full-40-char-commit-sha>
        with:
          artifact-stem: subcontractor-accounting-skills
          skills-verification-mode: subcontractor-accounting-v1

Replace `<full-40-char-commit-sha>` with a reviewed literal 40-character
commit before committing either consumer workflow.

The `subcontractor-accounting-v1` verifier requires a tracked regular version
file, `VERSION` by default, plus tracked regular files
`requirements-test.txt`, `scripts/validate_validation.py`,
`tests/verify_skills_cli.py` and at least one tracked regular `test*.py` file
under `tests/`. It runs these fixed commands in order:

1. `python -m pip install --isolated --disable-pip-version-check --no-input --no-deps --requirement requirements-test.txt`
2. `python -B -m unittest discover -s tests -v`
3. `python scripts/validate_validation.py`
4. `python tests/verify_skills_cli.py`

The verifier runs reviewed consumer code with only `contents: read`. It
declares no secrets or outputs, uses no cache and transfers no artefact or
other file to publication. A successful verifier is only a dependency gate.
The release adapter then starts publication on a new runner with exactly
`attestations: write`, `contents: write` and `id-token: write`.

`publish-archives.yml` is the internal privileged core called by
`release-archive.yml` and `release-skills.yml`; direct consumer calls are
unsupported. Consumers that advance their policy pin must verify attestations
against the new signer workflow,
`ryanduguid/release-policy/.github/workflows/publish-archives.yml`. The
source-archive family's inputs, fixed test command, assets and publication
behaviour are unchanged apart from that intentional signer change.

## Prerequisites in the consumer

- Annotated tags only; the human-created annotated tag is the release
  approval act. The module never creates tags.
- `RELEASE_NOTES.md` whose first line is `# vX.Y.Z` for the tag.
- `uv.lock` committed; the workflow runs `uv run --locked`.
- `pyproject.toml` with a static `[project] version`; or a safe relative,
  tracked Python file containing exactly one literal `__version__` assignment,
  selected with `version-parser: python-literal` and `version-file`.
- A `dev` extra in `pyproject.toml` providing `pytest` and `build`; the
  workflow runs `uv run --locked --extra dev`.
- A pure-Python wheel; the `py3-none-any` wheel name is expected by the
  SBOM and attestation steps.
- Releases cut from `main`; the main-match gate checks `heads/main`.

Source-archive callers additionally require:

- a lower-case hyphenated `artifact-stem`;
- a safe relative version file containing one canonical `MAJOR.MINOR.PATCH`
  line;
- a standard-library unittest suite runnable as
  `python -B -m unittest discover -s tests -v`; and
- no release-specific files that must be generated outside the four exact
  policy-owned assets.

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

Phase 2 and phase 3 designs are recorded under
[`docs/superpowers/specs/`](./docs/superpowers/specs/). Skill packs use a
separate adapter so their inventory and stronger validation controls are not
reduced to the source-archive contract. The historical notes-only `v0.1.0`
marker proves neither release phase.

## Release-family canaries

[`canaries.json`](./canaries.json) names one active Python, archive, skill and
verification consumer. Each entry binds the consumer's current literal policy
pin to the latest successful production-shaped run and the reusable workflow
SHA GitHub recorded for that run. `python scripts/check_canaries.py` validates
the manifest offline; the scheduled CI run adds `--live` to detect pin drift or
newer unrecorded successes.

A current pin can temporarily be newer than the latest release evidence. The
live audit reports that state explicitly without inventing a privileged dry
run. It becomes current evidence only when that consumer completes its next
authorised release through the pinned workflow.
