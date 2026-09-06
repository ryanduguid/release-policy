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
