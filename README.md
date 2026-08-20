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

A future module self-release requires a separately designed source-archive
path, deterministic assets, a `SHA256SUMS` file, user-facing verification
instructions, attestations where GitHub supports them, and immutability only
after the notes, assets, checksums, and attestations have been verified. These
are future conditions, not a description of a currently available release
path.

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

## Policy guarantees

- Fail closed: canonical semver tag, annotated tag object, tag commit equal
  to `main` via the GitHub API, clean tree, matching notes header, and no
  existing release for the tag, all verified before any build.
- Wheel and sdist built from the locked environment after the locked test
  suite passes.
- SPDX SBOM, verified `SHA256SUMS`, provenance attestation on every asset
  and an SBOM attestation on the wheel, draft-then-publish lifecycle.
- Consumers pin this repository by full commit SHA and upgrade by reviewed
  pull request (ADR-0001).
