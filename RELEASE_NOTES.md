# v0.5.0

`release-binary.yml` builds 2 more targets natively: Intel macOS
(`x86_64-apple-darwin`) and Windows arm64 (`aarch64-pc-windows-msvc`). A
release now holds 17 assets instead of 13. Every target is required: no input
leaves one out, so a consumer that moves its pin must build and pass its tests
on all 6, or stay on v0.4.0's commit. (#295)

`scripts/install.sh` (Linux and macOS) and `scripts/install.ps1` (Windows)
install a `release-binary.yml` release in one command. Each verifies
`SHA256SUMS` and the archive against the release's provenance, then the
archive's digest, before it installs anything; a failure installs nothing.
They pin neither the tagged commit nor the policy commit, so the manual check
in the binary guide remains the stronger one (#294). The opt-in
`--modify-path` (`install.sh`) or `-ModifyPath` (`install.ps1`) adds the
install directory to `PATH`: one line in the shell's startup file, or the user
`Path` on Windows (#295). `install.sh` picks the arm64 build under Rosetta,
finds a `ZDOTDIR` that `~/.zshenv` sets without exporting and accepts operands
after `--`; `install.ps1 -ModifyPath` refuses a folder holding `;` or `%`
(#300).

The new `release-policy init` command writes a repository's
`.github/workflows/release.yml` for a Python package, a Rust binary or a
source archive. It works offline, pins the caller to v0.4.0's commit and lists
what is left for the owner; `docs/quickstart.md` covers it (#296). Because it
pins v0.4.0, a binary caller it writes builds 4 targets, not 6. It refuses a
run anywhere but the top of a Git work tree (#298), an existing file unless
`--force`, a link on the path and a required check that names the workflow it
writes, on Windows through a hard link too (#300), and there it refuses a name
it cannot identify instead of accepting it (#301).

`.github/actions/secrets-scan` is a composite action that is the whole body of
a consumer's `secrets` job. It installs a pinned betterleaks release and scans
with shared rules for credentials and client identifiers, reading
`.gitleaks.toml` and `.gitleaksignore` from Git objects, never the work tree.
A pull request is scanned twice: with its own configuration, then with the
base's and inline allow comments ignored. Every scan passes `--text`, so a
file holding a NUL byte is no longer skipped. Consumers keep a byte-identical
mirror of the shared rules for local scans, and the action fails on a mirror
that differs. Callers pin a release tag's commit with the version in a
trailing comment, so Dependabot proposes the next release. The action accepts
`pull_request_target`; no consumer uses it yet. A weekly workflow here files
an issue when betterleaks has a newer release. `docs/secrets-scan-consumers.md`
is the guide. (#302)

The scan that guards what PR review sends to a model now runs Betterleaks
1.10.1 in place of Gitleaks 8.30.1: once with Gitleaks 8.30.1's rule file and
once with Betterleaks' rules, and a finding in either stops the request. A
line marked `gitleaks:allow` no longer passes the scan. The scripts'
`--gitleaks` option is now `--betterleaks`, and a snapshot stamped
`gitleaks/8.30.1` fails verification. The workflow now also downloads
Gitleaks' rule file from `raw.githubusercontent.com`, and a failed download
stops the review. `pr-review-central.yml` calls the workflow at a pinned
commit, so nothing changes until that pin moves. (#303)

Codecov's project check now fails below 100% and `main` requires it, so a pull
request cannot merge whilst Codecov is down, and one from a fork cannot merge
at all, because the upload job skips forks (#297, #299).

`canaries.json` records the v0.4.0 release run as the binary family's evidence
(#293). `release.yml` and the disposable binary canary now call
`release-binary.yml` at #302's merge commit, so the live canary audit fails
for the binary family until v0.5.0's release run is recorded.

Moving a binary pin to this release can fail a release that passed before:
the consumer's code and tests must build and pass on all 6 targets. No other
family changed since v0.4.0; `scripts/pin_families.py <old-commit>
<new-commit>` names the families that changed.

The `release-policy` executable ships for all 6 targets and gains `init`. The
workflows still run the Python scripts.

Known issues: the installers have not been run live on macOS or Windows on Arm,
and `init`'s hard-link check has not been run on FAT, exFAT or network
volumes. The secrets scan does not read text in another encoding, such as
UTF-16, or compressed files. No hosted run has yet exercised the PR review
scan's 2 downloads. The macOS archives are not notarised, and the
binary guide gives no Gatekeeper advice; a copy downloaded through a browser
may be blocked (not tried on a Mac). `virustotal-release.yml` uploads one
asset every 16 seconds and stops after 30 minutes, so it cannot scan a release
of more than about 100 assets. The version check in `release-archive.yml` and
`release-binary.yml` accepts a `VERSION` file that Git does not track or that
is a symbolic link to another file in the checkout, and reads an oversized one
whole before refusing it; the Python family refuses all 3.

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
A single `<parent>/*` line ships every skill directory directly in that
parent instead, built the same way and released together as one
`<stem>-<version>-skills.bundle.zip` asset; a parent line mixed with other
lines, or an unsafe, missing or file-only parent, or one holding a symbolic
link or submodule, refuses the release (#291).
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
audit. The binary family now records this repository's pin to #291's merge
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
than about 100 assets; a pack that lists every skill on its own
`skill-archives` line can pass that limit, so use the `<parent>/*` form. The version check in
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
