## Prerequisites in the consumer

Publication supports public GitHub repositories only. The final tag checks use
unauthenticated `git ls-remote`; private repositories fail closed. Do not use
these release adapters for a private consumer. Supporting private publication
requires a separately reviewed authentication and attestation contract.

- Annotated tags only; the human-created annotated tag is the release
  approval act. The module never creates tags.
- An active tag ruleset with update and deletion rules, including exactly
  `refs/tags/v*` (or `refs/tags/<tag-prefix>/v*` for a namespaced component) or
  `~ALL`, no exclusions and no bypass actors. SLSA v1.2 requires tags that cannot
  be moved or deleted at source level 2 and above. The release gates do not read
  rulesets; the live canary audit checks them for each release family in
  `canaries.json` and fails without one. GitHub shows a ruleset's bypass list only
  to a caller with write access to it, so the audit's own token reports a notice
  that it did not check the list, and fails on bypass actors only when a token can
  see them. Disabling a ruleset for a tag fix fails an audit that runs meanwhile.
- `RELEASE_NOTES.md` whose first line is `# vX.Y.Z` for the tag.
- `uv.lock` committed; the workflow runs `uv run --locked`.
- `pyproject.toml` with a static `[project] version`; or a safe relative,
  tracked Python file containing exactly one literal `__version__` assignment,
  selected with `version-parser: python-literal` and `version-file`.
- A `dev` extra in `pyproject.toml` providing `pytest` and `build`; the
  workflow runs `uv run --locked --extra dev`.
- The same `dev` extra listing every build requirement: each
  `[build-system] requires` entry exactly as written there, for example
  `hatchling==1.32.3`, and anything the backend reports it needs to build,
  such as a plugin, with `uv.lock` regenerated. The workflow installs the
  locked wheels without installing the project, then builds with
  `--no-isolation`, so the backend and its dependencies come hashed from the
  lock; a missing requirement stops the build before anything is published.
- A pure-Python wheel; the `py3-none-any` wheel name is expected by the
  SBOM and attestation steps.
- Releases cut from `main`'s head; the run's first job checks `heads/main`.
  Later gates accept a merge to `main` that leaves the component's source
  directory and the caller workflow untouched, so other components need no
  merge freeze once the first job's head check has passed.
- The tag-triggered workflow calls the release workflow here directly, with
  its policy pin and `required-checks` written in that file. The gates protect
  the file `GITHUB_WORKFLOW_REF` names; a local wrapper workflow between them
  would not be protected.
- A non-empty `required-checks` list naming the consumer's own mandatory
  checks as `<workflow path>: <job name>`. Those checks must have succeeded in
  a `push` or `workflow_dispatch` run of `main` for the exact release commit;
  the gate waits up to 10 minutes for a pending check and otherwise fails
  closed. Each named check must match exactly one job in its run, so give
  every mandatory job a name no other job in that run shares.
- `actions: read` on the calling job, beside the write permissions it already
  grants. The gate reads the consumer's own workflow runs and jobs. A called
  workflow cannot hold a permission its caller did not grant, so the
  `actions: read` the release workflows declare is a ceiling, not a grant: a
  caller that omits it leaves the gate unable to list runs and the release
  stops before the consumer's tests. The packaged-Python callers already grant
  it for the publication job's artefact inspection; archive and skill-pack
  callers must add it.

Run the [release preflight](release-preflight.md) against the actual candidate and
caller before seeking annotated-tag approval. It reuses the existing live check
verifier and must pass before the irreversible tag is prepared.

Source-archive callers additionally require:

- a lower-case hyphenated `artifact-stem`
- a safe relative version file containing one canonical `MAJOR.MINOR.PATCH`
  line;
- a standard-library unittest suite runnable as
  `python -B -m unittest discover -s tests -v`; and
- no release-specific files that must be generated outside the 4 exact
  policy-owned assets.
