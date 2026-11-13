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
          actions: read
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-skills.yml@<full-40-char-commit-sha>
        with:
          artifact-stem: subcontractor-accounting-skills
          skills-verification-mode: subcontractor-accounting-v1
          required-checks: |
            .github/workflows/verify.yml: lint
            .github/workflows/verify.yml: verify (3.14)

Replace `<full-40-char-commit-sha>` with a reviewed literal 40-character
commit before committing either consumer workflow. This prints the commit of
this repository's latest release:

    gh api repos/ryanduguid/release-policy/commits/$(gh api repos/ryanduguid/release-policy/releases/latest --jq .tag_name) --jq .sha

`required-checks` is required by the release adapter and has the same meaning
as for the [packaged-Python workflow](python-consumers.md): the named consumer
checks must have succeeded in a `push` or `workflow_dispatch` run of `main` for
the exact release commit, and every such run of the named workflow has to report
it as a success, with a bounded wait for a pending check and no other exception. Each
named check must match exactly one job in the run, so mandatory jobs need names
no other job in the run shares. The `guard` job runs the gate
before the shared verifier starts, so a tag on a commit whose own checks did
not pass never reaches verification or publication. The verification workflow
takes no such input.

`skill-archives` is optional. Each line names one tracked skill directory,
relative to the repository root, that ships as its own upload ZIP,
`<stem>-<version>-<directory name>.zip`. No form takes a parent directory, so
list every skill:

        with:
          skill-archives: |
            skills/payroll-check

Each ZIP is built from the tagged commit with `git archive`, like the source
archives, and holds one top-level folder, named after the directory, with
exactly one `SKILL.md` at its top. That is the layout OpenAI's
[skills cookbook](https://developers.openai.com/cookbook/examples/skills_in_api)
and Claude's help article
[How to create custom skills](https://support.claude.com/en/articles/12512198-how-to-create-custom-skills)
describe (both read 13 November 2026). Claude also expects the folder name to
match the `name` in `SKILL.md`, which the release does not check.
The release refuses the whole list if an entry is not a safe relative path, is
outside the source directory, has a folder name that is not lower-case and
hyphenated or repeats another's, has no tracked files, holds an executable,
symbolic link, submodule or `.gitattributes` file, has no `SKILL.md` at its top or has another, in
any letter case, below it. It also refuses a ZIP over 32 MB, VirusTotal's
upload limit. `virustotal-release.yml` uploads one asset every 16 seconds and
stops after 30 minutes, so it cannot scan a release of more than about 100
assets. The ZIPs are
listed in `SHA256SUMS` and covered by the provenance attestation, but not by
the SBOM attestation, which covers the source archives only.

`skills-verification-mode` is optional and defaults to the only supported
mode, `subcontractor-accounting-v1`. Both workflows still accept it, so the
lines above stay valid; a caller may drop them.

In mode `subcontractor-accounting-v1` the release adapter refuses the tag
`v0.1.0` and fails its `guard` job with `v0.1.0 is frozen and must never be
rebuilt or replaced`, because the mode treats a consumer's `v0.1.0` as a frozen
historical marker that no release may rebuild or replace, so a first skill-pack
release must carry some other tag, `v0.1.1` or later by convention.

The `subcontractor-accounting-v1` verifier requires a tracked regular
`VERSION` file, plus tracked regular files
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
