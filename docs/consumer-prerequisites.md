## Prerequisites in the consumer

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

Source-archive callers additionally require:

- a lower-case hyphenated `artifact-stem`;
- a safe relative version file containing one canonical `MAJOR.MINOR.PATCH`
  line;
- a standard-library unittest suite runnable as
  `python -B -m unittest discover -s tests -v`; and
- no release-specific files that must be generated outside the four exact
  policy-owned assets.
