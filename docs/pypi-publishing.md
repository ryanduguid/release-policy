## Publishing the same distribution to PyPI

PyPI cannot name a reusable workflow as a Trusted Publisher
([pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096)), so the
publish step runs in the consumer, not here. This module builds, tests, gates,
SBOMs and attests the distribution, then hands the exact same files to the
caller as a run artefact.

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
          required-checks: |
            .github/workflows/ci.yml: lint
            .github/workflows/ci.yml: test (3.12)

      pypi:
        name: publish to PyPI
        needs: release
        runs-on: ubuntu-latest
        environment:
          name: pypi
          url: https://pypi.org/p/<distribution-name>
        permissions:
          actions: read # Inspect the exact artefact.
          attestations: read # Verify publication attestations.
          contents: read # Read the checked-out policy.
          id-token: write # Mint the OIDC token PyPI Trusted Publishing verifies.
        steps:
          - name: Check out the policy that built the candidate
            uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
            with:
              repository: ryanduguid/release-policy
              ref: ${{ needs.release.outputs.policy-sha }}
              path: policy
              persist-credentials: false
          - name: Verify artefact identity
            env:
              GH_TOKEN: ${{ github.token }}
              DIST_ID: ${{ needs.release.outputs.dist-id }}
              DIST_DIGEST: ${{ needs.release.outputs.dist-digest }}
              STEM: ${{ needs.release.outputs.stem }}
              VERSION: ${{ needs.release.outputs.version }}
            run: |
              set -euo pipefail
              [[ "$DIST_ID" =~ ^[1-9][0-9]*$ ]]
              [[ "$DIST_DIGEST" =~ ^[0-9a-f]{64}$ ]]
              gh api "repos/$GITHUB_REPOSITORY/actions/artifacts/$DIST_ID" > artifact.json
              jq -e --argjson id "$DIST_ID" --arg digest "sha256:$DIST_DIGEST" \
                --arg name "dist-$STEM-$VERSION" --argjson run "$GITHUB_RUN_ID" \
                --arg sha "$GITHUB_SHA" \
                '.id == $id and .digest == $digest and .name == $name
                 and .expired == false and .workflow_run.id == $run
                 and .workflow_run.head_sha == $sha' artifact.json >/dev/null
          - name: Download the bound distribution
            uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
            with:
              artifact-ids: ${{ needs.release.outputs.dist-id }}
              path: candidate
          - name: Verify candidate bytes and publication attestations
            env:
              GH_TOKEN: ${{ github.token }}
              POLICY_SHA: ${{ needs.release.outputs.policy-sha }}
              STEM: ${{ needs.release.outputs.stem }}
              VERSION: ${{ needs.release.outputs.version }}
            run: |
              set -euo pipefail
              python policy/scripts/python_release.py verify-candidate \
                --directory candidate --stem "$STEM" --version "$VERSION" \
                --tag "$GITHUB_REF_NAME" --repository "$GITHUB_REPOSITORY" \
                --commit "$GITHUB_SHA" --policy-sha "$POLICY_SHA" \
                --run-id "$GITHUB_RUN_ID" --run-attempt "$GITHUB_RUN_ATTEMPT"
              mkdir dist
              for file in "candidate/$STEM-$VERSION-py3-none-any.whl" "candidate/$STEM-$VERSION.tar.gz"; do
                gh attestation verify "$file" --repo "$GITHUB_REPOSITORY" \
                  --source-digest "$GITHUB_SHA" --source-ref "$GITHUB_REF" \
                  --signer-workflow ryanduguid/release-policy/.github/workflows/release-python.yml \
                  --signer-digest "$POLICY_SHA"
                cp "$file" dist/
              done
          - name: Publish to PyPI
            uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2

The `required-checks` selectors above are an example. Replace them with the
consumer's own mandatory checks, each naming exactly one job of its run, as
[the packaged-Python guide](python-consumers.md) describes.

The `release` job exposes `stem`, `version`, `dist-id`, `dist-digest` and
`policy-sha`. The optional hand-off contains the wheel, sdist, SBOM, manifest
and SHA256SUMS. Download by ID and verify the run, commit and digest before
checking the candidate and its attestations. Only the verified wheel and sdist
enter the PyPI upload directory. Existing callers must adopt these steps when
upgrading their policy pin; their old name-only download does not meet this contract. Consumer tests and the build
run on separate `contents: read` jobs with sibling source and policy checkouts.
The publication job keeps source, policy and candidate data in 3 sibling
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
