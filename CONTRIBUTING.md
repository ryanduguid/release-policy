# Contributing

This repository publishes the reusable release workflows and the policy scripts
behind them. Every workflow and policy document is digest-pinned by
`tests/test_repository_baseline.sh`, so a change to one of them is a reviewed
policy change: update the digest in the same pull request and say why the
document changed. The digest reads a third-party action pin as its action and
major version only, so a Dependabot minor or patch bump passes without a digest
edit; a major bump, a new action or a pin to this repository still moves the
digest. Consumers pin a full commit SHA and migrate on their own reviewed
schedule.

## Local checks

Python 3.14 or newer; `scripts/python_release.py` reads pyproject metadata
with `tomllib`. CI runs the suite on Python 3.14 and 3.15. Install the pinned
tools, then run what `.github/workflows/ci.yml` runs:

```bash
python -m pip install --require-hashes --only-binary :all: --requirement requirements-ci.txt
python -m ruff check .
python -m mypy
python scripts/check_canaries.py
bash tests/test_repository_baseline.sh
bash tests/test_gates.sh
python -m coverage run --branch --source=scripts -m unittest discover -s tests -p "test_*.py" -v
python -m coverage xml --include="scripts/*.py" -o coverage.xml
diff-cover coverage.xml --compare-branch=origin/main --branch-coverage --fail-under=100
bash tests/test_determinism.sh
```

The input files pin direct tools; the generated requirements files pin their
dependencies and distribution hashes across supported Python versions and
platforms. After editing an input, regenerate both files with uv and review
the dependency changes:

```bash
uv pip compile requirements-build.in --universal --python-version 3.14 --generate-hashes --output-file requirements-build.txt
uv pip compile requirements-ci.in --universal --python-version 3.14 --generate-hashes --output-file requirements-ci.txt
```

`attribution-policy.yml` pins `.github/actions/no-ai-attribution` by commit.
After a change to that action merges, open a second pull request that repins
the workflow to the merge commit; `tests/test_attribution_pin.py` fails on
push until it lands, and every consumer then moves to the new policy commit.
The job token cannot push a workflow change, so the `Attribution repin`
workflow (`attribution-repin.yml`) prepares that repin after the merge, checks
that it turns the baseline and pin tests green, and files the patch as an issue
titled 'Repin the attribution action'. It refreshes its own open issue rather
than opening another. Apply the patch from a checkout whose credentials may
push workflow changes.

CI also runs pinned ShellCheck and actionlint over `scripts/*.sh`, `tests/*.sh`
and the workflows, and pinned zizmor over the workflows, actions and Dependabot
configuration. Record an accepted zizmor finding, with its reason, in
`.github/zizmor.yml`. Changed lines in `scripts/*.py` need complete branch
coverage.

The Rust CLI in `src/` reimplements the policy scripts; a change to a ported
script needs the same change in Rust. Run its checks with the toolchain that
`rust-toolchain.toml` pins:

```bash
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

[docs/rust-cli.md](docs/rust-cli.md) maps the scripts to commands and lists
the known differences.

Install the git hook once with `python -m pip install pre-commit && pre-commit install`; it runs the pinned ruff check on staged files and `cargo fmt --check`.

## Pull requests

Use fabricated or redacted evidence only, never a consumer's credentials or
private data. A release, tag or publication is authorised only through the
process the workflows encode, never by a pull request on its own. For a
potential security vulnerability follow [SECURITY.md](SECURITY.md).
