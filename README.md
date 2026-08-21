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

The repository now contains a reviewed-in-code source-archive workflow design
and implementation candidate. That does not retrofit `v0.1.0`, make the module
itself distributable, or prove a live consumer release. A source-archive
consumer migration remains a separate reviewed change pinned to a full policy
commit.

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
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-python.yml@<full-40-char-commit-sha>

Projects whose `pyproject.toml` declares `dynamic = ["version"]` add:

        with:
          version-command: "python -c 'from your_package.version import __version__; print(__version__)'"

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
the artifact without repeating the version logic.

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

## Prerequisites in the consumer

- Annotated tags only; the human-created annotated tag is the release
  approval act. The module never creates tags.
- `RELEASE_NOTES.md` whose first line is `# vX.Y.Z` for the tag.
- `uv.lock` committed; the workflow runs `uv run --locked`.
- `pyproject.toml` with a static `[project] version`, or the
  `version-command` input.
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
- Wheel and sdist built from the locked environment after the locked test
  suite passes.
- SPDX SBOM, verified `SHA256SUMS`, provenance attestation on every asset
  and an SBOM attestation on the wheel, draft-then-publish lifecycle.
- The source-archive family preserves the exact candidate artefacts, verifies
  provenance and both archive SBOM attestations before publication, rechecks
  remote tag/main/release absence, binds inspection to the draft create URL and
  numeric release ID, and verifies immutable/latest release state afterwards.
- Consumers pin this repository by full commit SHA and upgrade by reviewed
  pull request (ADR-0001).

Phase 2 and phase 3 designs are recorded under
[`docs/superpowers/specs/`](./docs/superpowers/specs/). Skill packs remain a
separate future adapter so their inventory and stronger validation controls are
not reduced to the source-archive contract.
