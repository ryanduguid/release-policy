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
          actions: read
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-python.yml@<full-40-char-commit-sha>

Root callers need no new inputs: `source-directory` defaults to `.` and
`tag-prefix` defaults to an empty string, so their package paths and `vX.Y.Z`
tags are unchanged.

A package inside a monorepo selects its tracked component directory and tag
namespace explicitly:

    on:
      push:
        tags: ["payday-super-checker/v*"]
    jobs:
      release:
        uses: ryanduguid/release-policy/.github/workflows/release-python.yml@<full-40-char-commit-sha>
        with:
          source-directory: packages/payday-super-checker
          tag-prefix: payday-super-checker

The GitHub tag is then `payday-super-checker/vX.Y.Z`; the component-local
`RELEASE_NOTES.md` still begins with `# vX.Y.Z`.

Component identity is fail closed. A root release must use exactly the defaults
`source-directory: .` and an empty `tag-prefix`. A nested release must use a
non-empty `tag-prefix` equal to the final segment of `source-directory`. For a
Python release, the distribution name from project metadata is normalised to a
lower-case hyphenated identity and must equal that prefix. For a source-archive
release, `artifact-stem` must equal the prefix. These bindings prevent otherwise
valid inputs from selecting a sibling component's metadata or version file.

Projects whose `pyproject.toml` declares `dynamic = ["version"]` add:

        with:
          version-parser: python-literal
          version-file: your_package/version.py

The `python-literal` parser reads exactly one top-level literal string assigned
to `__version__`. It parses the tracked file as data and never imports or
executes consumer code. Arbitrary version commands are not supported.
