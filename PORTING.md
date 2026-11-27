# Wave 2 Rust port notes

This document records the Wave 2 Rust port of `scripts/repin_issue.py`,
`scripts/binary_release.py` and `scripts/verify_pypi.py` into the
`release-policy` binary on branch `ryan/rust-wave2-release-policy-2026-11-25`
off `430a873`. Existing Python scripts and reusable workflow callers stay intact
until a separate reviewed release cuts workflows over to the published binary.

## Parity contract

Each ported command preserves the Python script's CLI flags (plus underscored
aliases on the command name), exit codes, standard output bytes, standard error
behaviour, created/extracted files, Unix mode bits, HTTP request paths and
headers and external tool arguments (`git`, `gh` and `python -m
pypi_attestations`). Runtime refusals exit `1`; CLI usage errors exit `2`.

## Item correspondence

| Python source | Rust target | Notes |
| --- | --- | --- |
| `scripts/repin_issue.py` | `src/repin_issue.rs`, `src/main.rs` (`RepinIssue`) | Ports `WORKFLOW`, `ACTION`, `ACTION_DIRECTORY`, `BASELINE`, `TITLE`, `BOT`, `MAX_ISSUE_PAGES`, `canonical_digest`, `system_git`, `system_gh`, `pin_is_current`, `unchanged_since`, `prepare`, `issue_body`, `open_issues`, `publish` and `prepare`/`publish` CLI subcommands. |
| `scripts/binary_release.py` | `src/binary_release.rs`, `src/main.rs` (`BinaryRelease`) | Ports constants (`MAX_MEMBERS`, `MAX_MEMBER_BYTES`, `MAX_ARCHIVE_BYTES`, `MAX_DECOMPRESSED_BYTES`, `DOCUMENTS`, `REQUIRED_DOCUMENTS`, `WINDOWS_SYSTEM_DLLS`, `TARGETS`), `handoff_spec`, `asset_name`, `binaries`, `data_names`, `member_names`, `cargo_package`, `find_documents`, `resolve_path`, `find_data_files`, `package`, `verify`, `smoke`, `needed_libraries`, `macho_linkage`, `linkage`, `toolchain`, `receive`, `sbom_lists`, `handoff_names`, `digests`, `accept`, `trusted_bases`, `confined_path` and all 12 subcommands (`cargo-package`, `toolchain`, `package`, `verify`, `receive`, `smoke`, `linkage`, `macho-linkage`, `sbom-lists`, `digests`, `assets`, `accept`). |
| `scripts/verify_pypi.py` | `src/verify_pypi.rs`, `src/main.rs` (`VerifyPypi`) | Ports `PYPI`, `INTEGRITY_ACCEPT`, `MAX_RESPONSE_BYTES`, `ATTEMPTS`, `DELAY`, `fetch`/`fetch_with_config`, `certificate_commit`, `verify_attestations`, `verify_publication`/`verify_publication_with` and the `verify-pypi` CLI command. |

## Test correspondence

| Python tests | Rust tests | Differential parity |
| --- | --- | --- |
| `tests/test_repin_issue.py` | `tests/rust/repin_issue.rs` (7 tests) | `tests/parity/repin_issue.py` (`24` inputs compared, `0` unexplained differences) |
| `tests/test_binary_release.py` | `tests/rust/binary_release.rs` (5 tests) | `tests/parity/binary_release.py` (`37` inputs compared, `0` unexplained differences) |
| `tests/test_verify_pypi.py` | `tests/rust/verify_pypi.rs` (4 tests) | `tests/parity/verify_pypi.py` (`24` inputs compared, `0` unexplained differences) |

## Dependency and `deny.toml` changes

- `Cargo.toml`: added `flate2 = "1"` and `crc32fast = "1"` for deterministic
  USTAR `.tar.gz` and ZIP packaging/verification, plus `ureq = "3"` (with its
  default `rustls` and `webpki-roots` TLS stack) for `verify-pypi`.
- `deny.toml`: added `'BSD-3-Clause'`, `'CDLA-Permissive-2.0'`
  (`# Permissive licence for the webpki root certificate data.`) and `'ISC'`
  to `licenses.allow` after confirming `cargo deny --locked --config deny.toml
  check licenses sources advisories` passes with zero advisories or copyleft
  licences and static `x86_64-unknown-linux-musl` compilation passes on Linux.

## Intended divergences

- **Argument parsing and error formatting**: `clap` replaces `argparse` (usage
  errors still exit `2`, abbreviated long flags are refused) and runtime errors
  emit one line instead of a traceback: `binary_release: <message>`,
  `verify_pypi: <message>` or `release-policy: <message>` for `repin-issue`
  (whilst `repin-issue publish` preserves the unprefixed stale-main message).
- **`repin-issue` repository root**: `release-policy repin-issue` acts on the
  current working directory, matching `check-canaries` and `pin-families`,
  whereas `scripts/repin_issue.py` defaults to its script checkout parent.
- **Deflate stream bytes**: `flate2` defaults to the pure-Rust `miniz_oxide`
  backend (no `zlib-rs` or C `zlib` dependency) rather than Python's C `zlib`,
  so raw `.tar.gz` and `.zip` compressed bitstreams differ from Python even
  though extracted entries, payload bytes, timestamps and Unix permissions are
  identical.
- **Hidden test overrides on `verify-pypi`**: `--base-url`, `--attempts`,
  `--delay-ms` and `--ca-cert` are hidden test flags so integration and parity
  suites can hit a local loopback server quickly without calling live PyPI.
  `--base-url` is restricted to `https?://127\.0\.0\.1:[0-9]{1,5}`, and
  `--ca-cert` requires an `https://127.0.0.1:` base URL.
- **Sigstore bundle verification**: `verify-pypi` shells out to the first
  `python` on `PATH` (`verify_skills::python_on_path`) running
  `-m pypi_attestations verify pypi ...` with a `600` s timeout instead of
  reimplementing Sigstore verification in Rust.

## Quirks preserved

- **`repin-issue`**: `THIRD_PARTY_PIN` excludes case-insensitive `ryanduguid/`
  references (`eq_ignore_ascii_case`), matching `scripts/repin_issue.py` and
  `tests/test_repository_baseline.sh`; `\r\n` normalisation runs in
  `canonical_digest` and `prepare` only, whilst `publish --patch` decodes raw
  UTF-8 bytes verbatim; pagination stops at `MAX_ISSUE_PAGES = 10`; non-bot
  issues produce `left #<n>: a person opened it`; `publish` checks
  `unchanged_since` against `main` before calling `gh`.
- **`binary-release`**: enforces `MAX_MEMBERS = 12`, `MAX_MEMBER_BYTES = 512 MiB`,
  `MAX_DECOMPRESSED_BYTES = 512 MiB + 64 KiB`, `64 KiB` ZIP directory cap and
  `1024`-byte digest lines; refuses PAX extended headers, ZIP64 locators/extras,
  symlinks (checked before path confinement) and path traversal; keeps
  `handoff_spec` archive-before-SBOM order; clamps ZIP dates before 1980 to
  1980-01-01 and maps mode `0` entries to `0o644` on extraction; refuses fat
  Mach-O binaries, `LC_RPATH`, `LC_DYLD_ENVIRONMENT`, non-`/usr/lib/dyld`
  loaders and non-system Mach-O dylibs; checks the `22` pinned
  `WINDOWS_SYSTEM_DLLS`; prints a blank line when list subcommands have zero
  items; `receive` removes only the `dist/<asset>` file it created if later
  checks fail.
- **`verify-pypi`**: enforces all `5` input regexes and PEP 503 project
  normalisation (`[-_.]+` to `-`, lowercased); turns HTTP `404` into missing
  metadata; inspects `3xx` `Location` headers manually and refuses redirects
  outside `("https", "pypi.org")`; enforces a `16 MiB + 1` byte read cap;
  follows the `21`-attempt, `15` s retry schedule and stops early on extra
  served files; requires exactly one UTF8String DER extension for Fulcio OID
  `1.3.6.1.4.1.57264.1.13` and strict base64 decoding.

## Survey of `scripts/pr_review*.py`

The `7` PR-review scripts split into portable policy/evidence tooling and `2`
Python/venv-coupled modules:

- **`pr_review_receipts.py` (`123` lines)**: portable. Sanitised SHA-256,
  `Decimal` bounds checking, strict JSON key checks and atomic `fsync` journal
  writes map cleanly to Rust.
- **`pr_review_measurements.py` (`209` lines)**: portable with an exact-decimal
  helper. Reads bounded receipt journals (`MAX_JOURNAL_BYTES = 131072`) and
  aggregates totals/ratios using `Decimal(prec=700)` (`f64` is insufficient).
- **`pr_review_public.py` (`181` lines)**: portable. Public PR URL parsing,
  caller-context validation, GitHub REST capture and summary rendering rely on
  `pr_review` helpers and `ureq` + `rustls`.
- **`pr_review_benchmark.py` (`198` lines)**: split. `save_manifest` and
  `prepare_case` (via `pr_review.collect`) are portable; its `--run` path calls
  `pr_review_agent.review_snapshot` directly and hashes `.py` candidate sources.
- **`pr_review.py` (`724` lines)**: mostly portable. Policy validation, GitHub
  REST collection, `verify_patch`, `scan_context` (`betterleaks` subprocess),
  chunking, cost guards, markdown escaping and advisory status publication are
  all self-contained; only the `review` command imports
  `pr_review_agent.review_snapshot`.
- **`pr_review_agent.py` (`301` lines)**: stays in Python. Wraps the pinned
  `PR-Agent` checkout (`PR_AGENT_SHA = 06a2991c0ce6b055c77fbcbd7f09faf4c5acfcaf`)
  in-process via `PRReviewer._get_prediction`, `TokenHandler`, `settings` and
  Pydantic `PRReview` validation unless the prompt/model layer is moved behind
  a subprocess boundary.
- **`pr_review_install.py` (`109` lines)**: stays in Python. Inspects the
  installed Python venv with `importlib.metadata`, `packaging` and `uv.lock`
  sdist verification (`html2text`).

## Verification

Run on Linux (`ll`) with `RUSTUP_TOOLCHAIN=1.99.1`:

- `cargo fmt --check`
- `cargo clippy --locked --all-targets -- -D warnings`
- `cargo deny --locked --config deny.toml check licenses sources advisories`
- `cargo audit --deny unsound --deny yanked --file Cargo.lock`
- `cargo test --locked` (`429` tests passing: `55` unit tests in `src/lib.rs` and
  `374` integration tests across `17` `tests/rust/` suites)
- `cargo build --locked --release --bin release-policy`
- `cargo auditable build --locked --release --target x86_64-unknown-linux-musl --bin release-policy`
- `cargo llvm-cov --locked --lcov --output-path rust.lcov --fail-under-lines 92`
  (`95.28%` line coverage overall: `repin_issue.rs` `96.32%`,
  `binary_release.rs` `88.50%`, `verify_pypi.rs` `90.06%`)
- `python3.14 tests/parity/repin_issue.py --binary <release-policy>` (`24` inputs compared, `0` unexplained differences)
- `python3.14 tests/parity/binary_release.py --binary <release-policy>` (`37` inputs compared, `0` unexplained differences)
- `python3.14 tests/parity/verify_pypi.py --binary <release-policy>` (`24` inputs compared, `0` unexplained differences)
- `bash tests/test_repository_baseline.sh`, `bash tests/test_gates.sh`,
  `bash tests/test_determinism.sh`, `python3.14 -m unittest discover -s tests -p "test_*.py" -v`
  and `ruff check .`
