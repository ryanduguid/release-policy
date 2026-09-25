## Using the source-archive release workflow

Source-only callers use the separate family workflow:

    jobs:
      release:
        permissions:
          actions: read
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-archive.yml@<full-40-char-commit-sha>
        with:
          artifact-stem: accounting-excel-toolkit
          required-checks: |
            .github/workflows/ci.yml: test (3.12)

The root defaults above also apply to source archives. A nested source-only
component uses:

    jobs:
      release:
        permissions:
          actions: read
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-archive.yml@<full-40-char-commit-sha>
        with:
          artifact-stem: accounting-excel-toolkit
          source-directory: adapters/accounting-excel-toolkit
          tag-prefix: accounting-excel-toolkit
          required-checks: |
            .github/workflows/standard-library-components.yml: verify (adapters/accounting-excel-toolkit, 3.12)

`required-checks` has the same meaning as for the
[packaged-Python workflow](python-consumers.md): the named consumer checks must
have succeeded in a `push` or `workflow_dispatch` run of `main` for the exact
release commit, and every such run of the named workflow has to report it as a
success, with a bounded wait for a pending check and no other exception. Each named
check must match exactly one job in the run, so mandatory jobs need names no
other job in the run shares. It runs in a separate `checks` job that
never fetches the consumer tree, and `consumer-tests` waits on it, so the
Actions read scope the gate needs is never held by a job running consumer code.

The version file is always the tracked `VERSION` file. The workflow accepts
no arbitrary test command, build command or asset glob. It runs the fixed
unittest contract, builds deterministic ZIP and tar.gz source archives from
the tagged commit, generates an SPDX SBOM, and publishes exactly those 3
files plus `SHA256SUMS` after inspecting the exact draft returned by GitHub.

## Test dependency hashes

The optional tracked `requirements-test.txt` must pin each dependency with
`==` and include its reviewed wheel's SHA-256 hash. List any required transitive
dependencies explicitly because the workflow uses `--no-deps`. Installation
also uses `--only-binary :all:` and `--require-hashes`; a missing hash, mismatched
download or unavailable wheel fails before consumer tests run. Consumers with
no external test dependencies can omit the file.

Before adopting a policy revision with this requirement, add hashes to the
consumer's requirements file and verify that its wheels support Python 3.14 on
Linux. Review the requirements change with the policy-pin update. Existing
consumers remain on their current pinned policy until they migrate.
