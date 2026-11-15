# Quickstart: set up a release with `init`

`release-policy init` writes `.github/workflows/release.yml` for a repository
that ships a Python package, a Rust binary or a source archive. It works
offline: it reads the repository, pins the caller to a released policy commit
and refuses inputs the release jobs would refuse.

## 1. Install the command

`init` is on `main` but not in a release yet: v0.4.0 predates it. Build it
from a commit on `main` with Rust 1.99 or later:

```sh
cargo install --locked --git https://github.com/ryanduguid/release-policy --rev <full-40-char-commit-sha> --bin release-policy release-policy
```

Once a release includes `init`, install that release instead, after
verifying it: see [Installing a release](binary-consumers.md#installing-a-release).

## 2. Run it at the top of your repository

The repository must be a Git work tree, and for a Python package Git must
track `pyproject.toml` and any `--version-file`.

Name each check that must pass on `main` before a release, as
`<workflow path>: <job name>`, with the job name GitHub shows:

```sh
release-policy init --required-check ".github/workflows/ci.yml: test"
```

It picks the release family from what it finds:

| It finds | Family | Workflow called |
| --- | --- | --- |
| `pyproject.toml` with a `[project]` table | `python` | `release-python.yml` |
| `Cargo.toml` with a `[package]` table | `binary` | `release-binary.yml` |
| Neither, but a `VERSION` file | `archive` | `release-archive.yml` |

When it finds both manifests, pass `--family python` or `--family binary`.
Other options:

- `--version-file <path>`: for a Python package whose `pyproject.toml`
  declares `dynamic = ["version"]`, the file holding one literal
  `__version__`.
- `--artifact-stem <stem>`: the release assets' name. A Rust binary defaults
  to its package name with `-` for `_`, and an archive to the repository
  directory's name.
- `--force`: replace an existing `release.yml`. Without it, `init` refuses
  and changes nothing.

It refuses to write what the release would refuse: a malformed or duplicate
check, a check whose workflow file is not in the repository, a stem that is
not lower-case and hyphenated, or Python metadata the release cannot read. It
also refuses a check or version file that is `release.yml` itself, and a
check holding `${{`, which GitHub would evaluate before the release saw it.
It does not write a caller for a component inside a monorepo or for a skill
pack, or a PyPI publish job (step 3); copy those callers from
[Packaged Python](python-consumers.md),
[Source archives](archive-consumers.md) or
[Skill verification and releases](skill-consumers.md).

## 3. Do what it lists

`init` ends by listing what only the repository's owner can do. For every
family:

- Commit the workflow to `main`.
- Protect release tags with an active tag ruleset on `refs/tags/v*`, with
  update and deletion rules and no bypass actors (repository settings, Rules,
  Rulesets).
- For each release, start `RELEASE_NOTES.md` with `# vX.Y.Z`, let the
  required checks pass on `main`'s head, then push an annotated tag `vX.Y.Z`
  there.

No secret is needed: the release runs on the workflow's own token. Publishing
the same Python package to PyPI as well needs a Trusted Publisher on PyPI and
a job in your own workflow, as [PyPI hand-off and one-time setup](pypi-publishing.md)
describes. [Prerequisites](consumer-prerequisites.md) lists each family's
requirements, and the [release preflight](release-preflight.md) checks a
candidate before you tag it.
