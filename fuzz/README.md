The Atheris target exercises release-tag identity, required-check selectors and
relative-path parsing with fabricated bytes. It checks canonical round trips,
duplicate rejection and path containment. Expected `ValueError` rejections are
allowed; unexpected exceptions and failed invariants fail the run.

On Linux with Python 3.12, run:

```sh
python -m pip install --require-hashes --only-binary :all: -r requirements-fuzz.txt
python -m fuzz.policy_parsers fuzz/corpus -max_total_time=60 -max_len=4096 -timeout=2 -rss_limit_mb=1024
```

CI runs the target on every PR and main push, and on the existing weekly
schedule. Its job has read-only repository access and a five-minute timeout.
The fuzzer calls pure parsers; it does not inspect consumer files, call GitHub or
publish releases. This target does not cover filesystem validation, network
responses or the publication process. A successful run is bounded evidence,
not proof that every input is safe.

The checked-in corpus covers valid root and component tags, required checks,
duplicate selectors, traversal, invalid UTF-8 and empty input. The unittest
suite replays it on all supported platforms and verifies that unexpected
exceptions and injected invariant failures reach the caller. Atheris itself
does not support Windows, so coverage-guided fuzzing runs on Linux.

After changing the pinned version, regenerate its hashes with:

```sh
uv pip compile requirements-fuzz.in --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes --output-file requirements-fuzz.txt
```
