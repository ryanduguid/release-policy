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

In mode `subcontractor-accounting-v1` the release adapter refuses the tag
`v0.1.0` and fails its `guard` job with `v0.1.0 is frozen and must never be
rebuilt or replaced`, because the mode treats a consumer's `v0.1.0` as a frozen
historical marker that no release may rebuild or replace, so a first skill-pack
release must carry some other tag, `v0.1.1` or later by convention.

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
