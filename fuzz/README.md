The Atheris target exercises release-tag identity, required-check selectors and
relative-path parsing with fabricated bytes. It checks canonical round trips,
duplicate rejection and path containment. Expected `ValueError` rejections are
allowed; unexpected exceptions and failed invariants fail the run. A cargo-fuzz
target, described below, applies the same checks to the Rust CLI's parsers.

On Linux with Python 3.14, run from the repository root:

```sh
python -m pip install --require-hashes --only-binary :all: -r requirements-fuzz.txt
python -m fuzz.policy_parsers fuzz/corpus -max_total_time=60 -max_len=4096 -timeout=2 -rss_limit_mb=1024
```

CI runs the target after main pushes, on the existing weekly schedule and on
demand. Its job has read-only repository access and a 5-minute timeout.
The fuzzer calls pure parsers; it does not inspect consumer files, call GitHub or
publish releases. This target does not cover filesystem validation, network
responses or the publication process. A successful run is bounded evidence,
not proof that every input is safe.

`fuzz/` is also a separately locked cargo-fuzz crate with one libFuzzer target,
`policy_parsers`, which calls `release_policy::fuzz::check_input` on the same
corpus. On Linux, with the `nightly-2026-10-15` toolchain and cargo-fuzz 0.13.2
that CI pins, run from the repository root:

```sh
cargo +nightly-2026-10-15 fuzz run --target x86_64-unknown-linux-gnu policy_parsers fuzz/corpus -- -max_total_time=60 -max_len=4096 -timeout=2 -rss_limit_mb=1024
```

The cargo-fuzz release binary is built for musl and defaults to that target,
where the address sanitiser cannot build, so the command names the glibc
target. CI runs it on the same triggers as the Atheris target, with read-only
repository access and a 20-minute timeout.

The checked-in corpus covers valid root and component tags, required checks,
duplicate selectors, traversal, invalid UTF-8 and empty input. The unittest
suite replays it in CI on Linux with Python 3.14 and 3.15, and
`tests/rust/fuzz_harness.rs` replays it for the Rust CLI on Linux and Windows.
Both verify that unexpected parser errors (exceptions in Python, panics in
Rust) and injected invariant failures reach the caller. Atheris itself does
not support Windows, so coverage-guided fuzzing runs on Linux.

After changing the pinned version, regenerate its hashes with:

```sh
uv pip compile requirements-fuzz.in --python-version 3.14 --python-platform x86_64-unknown-linux-gnu --generate-hashes --output-file requirements-fuzz.txt
```
