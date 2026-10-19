# The Rust CLI

`release-policy` is a single Rust binary that reimplements the policy scripts
under `scripts/`. It is built and tested in CI next to them, but no reusable
workflow calls it yet: the workflows keep running the Python scripts until a
release of the binary is published and a reviewed change switches them over.

## Build and check

The toolchain is pinned in `rust-toolchain.toml`. Run what the `rust` job in
`.github/workflows/ci.yml` runs:

```bash
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked --release --bin release-policy
```

On Linux CI measures line coverage with `cargo llvm-cov` and fails below the
floor written in the workflow. The `rust-fuzzing` job runs the cargo-fuzz
target in `fuzz/` over the same corpus as the Python fuzzer.

`tests/rust/data/python_goldens.json` records the Python scripts' answers for
the pure parsers; `tests/rust/goldens.rs` requires the binary's library to
give the same answers. The `fake-gh` binary built from
`tests/rust/fake_gh.rs` stands in for `gh` in the tests.

## Commands

Each script becomes a subcommand with the same arguments, exit codes, output
bytes and file formats. Underscored names are accepted as aliases.

| Script | Command |
| --- | --- |
| `scripts/attribution_status.py` | `release-policy attribution-status` |
| `scripts/build_release_archives.py` | `release-policy build-release-archives` |
| `scripts/check_canaries.py` | `release-policy check-canaries` |
| `scripts/check_egress.py` | `release-policy check-egress` |
| `scripts/check_reviews.py` | `release-policy check-reviews` |
| `scripts/pin_families.py` | `release-policy pin-families` |
| `scripts/python_release.py` | `release-policy python-release` |
| `scripts/required_checks.py` | `release-policy required-checks` |
| `scripts/verify_skills.py` | `release-policy verify-skills` |

For example, `python scripts/python_release.py tag --tag v1.2.3 --tag-prefix v`
becomes `release-policy python-release tag --tag v1.2.3 --tag-prefix v`.

The PR-review scripts (`scripts/pr_review*.py`) stay in Python: they drive the
PR-Agent engine in-process and share its virtual environment.
`.github/ci/check_gates.py` stays Python because consumers copy it byte for
byte. The shell scripts stay shell.

## Differences from the scripts

These are the only known behaviour differences. Each keeps the exit code.

- Usage errors and `--help` text come from clap, not argparse; usage errors
  still exit 2. Abbreviated long options, such as `--ver` for `--version`, are
  refused.
- An unexpected Python exception printed a traceback. The binary prints one
  line, `release-policy: <message>` or `<command>: <message>`, with the same
  exit code. Where the script raised `ValueError` or `FileExistsError` the
  message text is the same.
- `check-canaries` reads `canaries.json` in the working directory by default,
  and `pin-families` reads the Git repository in the working directory. The
  scripts used their own checkout.
- `verify-skills` runs the first `python` on `PATH`, where the script ran its
  own interpreter.
- JSON nested deeper than the Rust parser's fixed limit is refused. Python's
  limit depended on the C stack.
- A JSON `\u` escape for a lone UTF-16 surrogate is refused when read.
- A TOML integer outside the 64-bit range or a float that overflows to
  infinity makes `pyproject.toml` invalid; `tomllib` accepted both.
