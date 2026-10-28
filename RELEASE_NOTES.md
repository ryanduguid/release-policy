# v0.3.0

Relicensed from Apache-2.0 to FSL-1.1-ALv2 (Functional Source License, with
Apache-2.0 as the future licence). Released versions keep their licence.

Added `release-binary.yml`, a reusable release family for Rust components:
tested Linux musl and Windows executables with SBOMs and attestations,
published with the source archives under one tag.

First release with the `release-policy` executable, built and published by
`release-binary.yml` itself. The workflows still run the Python scripts.

# v0.1.0

The repository's [GitHub Releases](https://github.com/ryanduguid/release-policy/releases) page is the canonical release history. A separate changelog is intentionally not maintained.

Initial module: release-readiness gates (`scripts/gates.sh`), the
packaged-Python reusable release workflow, module CI, and consumer
documentation. Governed by ADR-0001.
