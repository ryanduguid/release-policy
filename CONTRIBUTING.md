# Contributing

This repository publishes the reusable release workflows and the policy scripts
behind them. Every workflow and policy document is digest-pinned by
`tests/test_repository_baseline.sh`, so a change to one of them is a reviewed
policy change: update the digest in the same pull request and say why the
document changed. Consumers pin a full commit SHA and migrate on their own
reviewed schedule.

## Local checks

Python 3.11 or newer; `scripts/python_release.py` reads pyproject metadata
with `tomllib`, which arrived in 3.11, and CI runs the suite on 3.11, 3.12 and
3.13. Install the pinned tools, then run what `.github/workflows/ci.yml` runs:

```bash
python -m pip install "build==1.2.2" "coverage==7.15.4" "diff-cover==10.5.1" "ruff==0.16.6" "mypy==2.3.1"
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

CI also runs pinned ShellCheck and actionlint over `scripts/*.sh`, `tests/*.sh`
and the workflows. Changed lines in `scripts/*.py` need complete branch
coverage.

Install the git hooks once with `python -m pip install pre-commit && pre-commit install`; they run the pinned ruff check and ruff format on staged files.

## Pull requests

Use fabricated or redacted evidence only, never a consumer's credentials or
private data. A release, tag or publication is authorised only through the
process the workflows encode, never by a pull request on its own. For a
potential security vulnerability follow [SECURITY.md](SECURITY.md).
