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
        with:
          required-checks: |
            .github/workflows/ci.yml: lint
            .github/workflows/ci.yml: test (3.12)

Root callers need no other inputs: `source-directory` defaults to `.` and
`tag-prefix` defaults to an empty string, so their package paths and `vX.Y.Z`
tags are unchanged.

`required-checks` names the consumer checks that must have succeeded for the
exact release commit, one per line as `<workflow path>: <job name>`, where the
job name is the check name GitHub shows (`lint`, `test (3.12)`,
`payday-super-checker / test (3.12)`). The gate reads the consumer's workflow
runs for that commit, counts only `push` and `workflow_dispatch` runs of `main`
in the consumer repository, judges each check from the newest such run of its
workflow, and waits up to 10 minutes for a check that is still queued or in
progress. A check that is missing, skipped, cancelled, failed, still pending
after the wait, or reported only by a pull-request run or another workflow file
stops the release before the consumer's tests or build run. The list may not be
empty. The gate runs in its own `checks` job, which holds the Actions read
scope it needs and never checks out the consumer tree, so no job that executes
consumer code carries that scope. Name the release's own component checks, not a monorepo's aggregate
`<engine> / gates` job: that job passes when the engine's tests were skipped by
a path filter, which is exactly what this gate exists to refuse.

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
          required-checks: |
            .github/workflows/ci.yml: lint
            .github/workflows/ci.yml: payday-super-checker / lint
            .github/workflows/ci.yml: payday-super-checker / test (3.12)
            .github/workflows/ci.yml: payday-super-checker / build

The GitHub tag is then `payday-super-checker/vX.Y.Z`; the component-local
`RELEASE_NOTES.md` still begins with `# vX.Y.Z`.

Component identity is fail closed. A root release must use exactly the defaults
`source-directory: .` and an empty `tag-prefix`. A nested release must use a
non-empty `tag-prefix` equal to the final segment of `source-directory`. For a
Python release, the distribution name from project metadata is normalised to a
lower-case hyphenated identity and must equal that prefix. For a source-archive
release, `artifact-stem` must equal the prefix. These bindings prevent otherwise
valid inputs from selecting a sibling component's metadata or version file.

### Migrating an existing caller

`required-checks` is a required input on all three release families, so a
caller that advances its pin to a policy commit carrying this gate must add the
input in the same reviewed pull request. GitHub refuses a reusable-workflow
call that omits a required input, so the two changes cannot be split: bumping
the pin alone breaks the caller's next release, and adding the input alone
breaks it against the older pin, which does not define it. Callers that have
not yet advanced their pin are unaffected and keep working unchanged.

Choose the listed checks from a real successful run of the consumer's own CI on
`main`, not from the branch-protection context list. The two differ: protection
lists the aggregate `<engine> / gates` context, which reports success when the
engine's own jobs were skipped by a path filter.

Projects whose `pyproject.toml` declares `dynamic = ["version"]` add:

        with:
          version-parser: python-literal
          version-file: your_package/version.py

The `python-literal` parser reads exactly one top-level literal string assigned
to `__version__`. It parses the tracked file as data and never imports or
executes consumer code. Arbitrary version commands are not supported.
