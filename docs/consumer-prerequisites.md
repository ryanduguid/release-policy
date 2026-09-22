## Prerequisites in the consumer

Publication supports public GitHub repositories only. The final tag checks use
unauthenticated `git ls-remote`; private repositories fail closed. Do not use
these release adapters for a private consumer. Supporting private publication
requires a separately reviewed authentication and attestation contract.

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

Source-archive callers additionally require:

- a lower-case hyphenated `artifact-stem`
- a safe relative version file containing one canonical `MAJOR.MINOR.PATCH`
  line;
- a standard-library unittest suite runnable as
  `python -B -m unittest discover -s tests -v`; and
- no release-specific files that must be generated outside the 4 exact
  policy-owned assets.
