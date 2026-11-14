# v0.4.0

Versions made available from 4 November 2026 are licensed under the Ryan
Duguid Public Attribution Licence, Version 1.0
(LicenseRef-Duguid-Public-Attribution-1.0): the FSL-1.1-ALv2 text with a
public attribution condition. A copy, port or derivative made available
outside your organisation must credit Ryan Duguid publicly; internal use is
exempt, and the condition does not apply to a version's Apache-2.0 licence.
Released versions keep their licence. (#265, #266, #267)

`release-binary.yml` builds 2 more targets natively: Linux arm64
(`aarch64-unknown-linux-musl`) and macOS arm64 (`aarch64-apple-darwin`).
`binary` can name several Cargo binary targets, and the new `data-files` input
ships run-time files beside them. A release now holds 13 assets instead of 9.
Intel macOS is not built. (#278)

The binary family runs Syft, rustup and `cargo-audit` in a read-only `admit`
job, and the publisher admits each archive again (#258). Any RustSec advisory
in the executables' embedded dependency lists, informational ones included,
refuses the release; there is no ignore list (#231). The build sets
`RELEASE_SOURCE_COMMIT` to the released commit (#286), and path arguments
outside the runner's directories are refused (#230).

`release-skills.yml` gains an optional `skill-archives` input that ships each
listed skill as its own upload ZIP, listed in `SHA256SUMS` and covered by the
provenance attestation; a skill ZIP over 32 MB refuses the release (#288).
Source archives are now built from the tagged commit's tree, so a component
moved or removed since can still be archived (#257).

Every release family's run summary lists its assets, their SHA-256 digests
and the `gh attestation verify` command (#273). The gate runners stop at any
failed gate even when called as a condition (#279), and the first gate refuses
an expected commit that is not a full SHA (#282). The PyPI guide adds a
`pypi-readback` job that fails unless PyPI serves exactly the built files,
with provenance from your `release.yml` at the release commit (#290).

The attribution check refuses the trailer forms current tools write (#270,
#272). Its guide tells public consumers to allow `pull_request_target` before
GitHub blocks it by default on 2 December 2026 (#260).

The live canary audit admits a consumer pin that is off `main` when a locked
release tag of this repository reaches it and a commit on `main` carries the
same tree, and names that commit as the repin target (#248). The weekly
RustSec audit of release executables runs in a job of its own, so a failing
pin check no longer skips it, and checks each archive against `SHA256SUMS` and
its provenance first (#256); it now audits each binary canary's recorded
release (#278). CI and CodeQL runs outside pull requests are grouped by
commit, so a later merge no longer cancels an earlier commit's run on `main`
(#289). The Snyk workflow, its policy file and its README badge are removed
(#243).

`canaries.json` records new consumer releases at `ca5492b` for the archive,
Python and skills families, whose missing evidence has failed the weekly live
audit. The binary family now records this repository's pin to #290's merge
commit with the v0.3.0 run as its evidence, so the audit will fail at least
until v0.4.0's release run is recorded.

New guides cover verifying a downloaded release (#244) and what
release-policy is, when another tool fits better and which SLSA level it is
designed for (#274). Each consumer guide shows its full caller and the command
that prints the latest release commit (#284).

Moving a pin to this release can fail a release that passed before: binary
consumers' code and tests must build and pass on all 4 targets, a RustSec
advisory refuses the release, and more attribution trailers are refused.
`scripts/pin_families.py <old-commit> <new-commit>` names the families that
changed.

The `release-policy` executable ships for the same 4 targets. The workflows
still run the Python scripts. The executable now prints floats as Python's
`repr` does, and a failed write to stdout or stderr fails the run (status 120
for output still pending at exit) instead of exiting with the normal code, as
the scripts do (#228).

Known issues: the macOS arm64 archives are not notarised, and the binary guide
gives no Gatekeeper advice; a copy downloaded through a browser may be
blocked (not tried on a Mac). `virustotal-release.yml` uploads one asset every
16 seconds and stops after 30 minutes, so it cannot scan a release of more
than about 100 assets; `skill-archives` has no parent-directory form, so a
pack that lists every skill can pass that limit. The version check in
`release-archive.yml` and `release-binary.yml` accepts a `VERSION` file that
Git does not track or that is a symbolic link to another file in the
checkout, and reads an oversized one whole before refusing it; the Python
family refuses all 3.

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
