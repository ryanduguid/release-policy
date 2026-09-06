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

The root defaults above also apply to source archives. A nested source-only
component uses:

    jobs:
      release:
        uses: ryanduguid/release-policy/.github/workflows/release-archive.yml@<full-40-char-commit-sha>
        with:
          artifact-stem: accounting-excel-toolkit
          source-directory: adapters/accounting-excel-toolkit
          tag-prefix: accounting-excel-toolkit

`version-file` defaults to `VERSION`. The workflow accepts no arbitrary test
command, build command or asset glob. It runs the fixed unittest contract,
builds deterministic ZIP and tar.gz source archives from the tagged commit,
generates an SPDX SBOM, and publishes exactly those three files plus
`SHA256SUMS` after inspecting the exact draft returned by GitHub.
