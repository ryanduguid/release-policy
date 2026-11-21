<p align="center">
  <picture>
    <source media="(max-width: 600px) and (prefers-color-scheme: light)" srcset=".github/assets/banner-phone-light.svg">
    <source media="(max-width: 600px)" srcset=".github/assets/banner-phone.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/banner-light.svg">
    <img src=".github/assets/banner.svg" width="100%" alt="Release Policy. Releases with provenance, by reusable workflow. Pixel art of a wax seal with a star and ribbons beside the Release Policy wordmark in slab-serif block letters.">
  </picture>
</p>

<div align="center">

[![CI](https://github.com/ryanduguid/release-policy/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ryanduguid/release-policy/actions/workflows/ci.yml) [![Codecov](https://codecov.io/gh/ryanduguid/release-policy/graph/badge.svg)](https://codecov.io/gh/ryanduguid/release-policy) [![CodeQL](https://github.com/ryanduguid/release-policy/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/ryanduguid/release-policy/actions/workflows/codeql.yml) [![SonarCloud](https://sonarcloud.io/api/project_badges/measure?project=ryanduguid_release-policy&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=ryanduguid_release-policy) [![Codacy code quality](https://app.codacy.com/project/badge/Grade/49440ac0ee144a12b5fc1380425e9773)](https://app.codacy.com/gh/ryanduguid/release-policy/dashboard)<br> [![OpenSSF Scorecard](https://raw.githubusercontent.com/ryanduguid/.github/badges/ryanduguid/release-policy.svg)](https://scorecard.dev/viewer/?uri=github.com/ryanduguid/release-policy) [![OpenSSF Best Practices](https://www.bestpractices.dev/projects/15422/badge)](https://www.bestpractices.dev/projects/15422) [![SLSA build provenance](.github/assets/badges/slsa.svg)](https://github.com/ryanduguid/release-policy/attestations)<br> [![release](https://badgen.net/github/release/ryanduguid/release-policy/stable?label=release&color=66023C&labelColor=000000)](https://github.com/ryanduguid/release-policy/releases/latest) [![licence: Apache-2.0](.github/assets/badges/licence.svg)](LICENSE)

[Quickstart](docs/quickstart.md) · [Prerequisites](docs/consumer-prerequisites.md) · [Verify a release](docs/verify-a-release.md) · [Guarantees](docs/guarantees-and-evidence.md) · [Canaries](canaries.json) · [Alternatives](#alternatives) · [Development](#development) · [Licence](#licence)

</div>

release-policy is a set of reusable GitHub Actions workflows for repository maintainers: they release Python packages, source archives, Rust binaries and skill packs with checksums and SLSA provenance, and run VirusTotal release scans, attribution checks and Claude PR reviews.

A consumer calls one with `uses:`, pinned to a full commit SHA of this repository. Before adopting, check 3 limits:

- **PyPI:** PyPI cannot name a reusable workflow as a Trusted Publisher ([pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096)), so the [publish step](docs/pypi-publishing.md) runs in the consumer, not here.
- **Targets:** `release-binary.yml` builds Linux (static musl), macOS and Windows, each for x86_64 and arm64 only; no other operating system or architecture is built, and a release needs all 6 to build and pass their tests. v0.4.0, which `init` pins, predates the macOS x86_64 and Windows arm64 targets, so it builds 4. [Alternatives](#alternatives) says when another tool fits better.
- **SLSA:** The release workflows are designed to support SLSA Build Level 3, but no audit has shown that a release reaches it. [SLSA Build Level](docs/guarantees-and-evidence.md#slsa-build-level) says what is unverified and why verification needs `--signer-workflow`.

| Workflow | Use it to | Guide |
| --- | --- | --- |
| `release-python.yml` | Build, attest and hand a Python package to PyPI | [Packaged Python](docs/python-consumers.md) |
| `release-archive.yml` | Publish a checksummed, attested source archive | [Source archives](docs/archive-consumers.md) |
| `release-binary.yml` | Build and publish Rust executables for the targets above | [Rust binaries](docs/binary-consumers.md) |
| `release-skills.yml`, `verify-skills.yml` | Verify and release an agent skill pack | [Skill verification and releases](docs/skill-consumers.md) |
| `virustotal-release.yml` | Scan a release's assets with VirusTotal | [VirusTotal release scans](docs/virustotal-consumers.md) |
| `attribution-policy.yml` | Fail PR text and commits that carry AI attribution credits | [Attribution checks](docs/attribution-consumers.md) |
| `pr-review.yml` | Run Claude PR reviews with your own API key | [PR reviews](docs/pr-review.md) |

`scripts/install.sh` and `scripts/install.ps1` install a Rust binary release in one command, and only after its checksum and build provenance verify: see [Installing a release](docs/binary-consumers.md#installing-a-release).

Policy checks establish release evidence, not accounting correctness. The example below is a recorded canary.

**Input:** the recorded `v0.4.0` Python canary from AU FP&A Pack.

The consumer's release caller at that tag is:

```yaml
uses: ryanduguid/release-policy/.github/workflows/release-python.yml@0f7ca4ce0ba2e7ea52fc611149a42cd5d0a26f6c
```

**Recorded output:** run 40612457692 on 28 October 2026 supplies the release evidence for [au-fpa-pack v0.4.0](https://github.com/ryanduguid/au-fpa-pack/releases/tag/v0.4.0): the wheel, source archive, SBOM, provenance, release manifest and `SHA256SUMS`, with policy SHA `0f7ca4ce0ba2e7ea52fc611149a42cd5d0a26f6c`.

**Human decision:** Check the release artefacts and their attestations against that recorded policy revision. This evidence applies to the recorded policy revision. Later policy changes need their own verification.

This snapshot was checked against [canaries.json](canaries.json) on 2 November 2026. The manifest retains the source commit, policy SHA and run identity; consult it for later updates.

To set up a release, run [`release-policy init`](docs/quickstart.md): it writes the release workflow for a Python package, Rust binary or source archive and lists what is left for the owner. Check the [consumer prerequisites](docs/consumer-prerequisites.md) before choosing a release workflow.

<details>
<summary>Consumer setup, policy guarantees and historical limits</summary>

## Release status

[`v0.5.0`](https://github.com/ryanduguid/release-policy/releases/tag/v0.5.0)
is the latest release: the `release-policy` executable for Linux, macOS and
Windows, each for x86_64 and arm64, with SBOMs, provenance attestations and
`SHA256SUMS`, built and published by this repository's own `release-binary.yml`.
The workflows still run the Python scripts, not that executable. It is the first
release with the `init` command, the installers and the secrets scan action.

[`v0.4.0`](https://github.com/ryanduguid/release-policy/releases/tag/v0.4.0)
built 4 targets, without Intel macOS and Windows arm64, and was the first
release under the Ryan Duguid Public Attribution Licence 1.0; earlier releases
keep their own licence.

[`v0.3.0`](https://github.com/ryanduguid/release-policy/releases/tag/v0.3.0)
(28 October 2026) was the first release with an artefact, for Linux and Windows
only, and the first FSL-1.1-ALv2 release.

`v0.1.0` and `v0.2.0` are notes-only source markers with no uploaded artefact.
`v0.2.0` marks `a601f1d8b143f92b0517f709216993660d21f480`, the revision ryanduguid
source repositories pinned on 19 October 2026. GitHub marks the releases
immutable, so their tags and assets are fixed; the markers must not be
retrofitted with assets or described as distributables.

Each consumer migration remains a separate reviewed change pinned to a full
policy commit.

## Consumer guides

The release-policy guides are grouped as how-to, reference and background pages.

How-to:

- [Quickstart: set up a release with init](docs/quickstart.md)
- [Packaged Python](docs/python-consumers.md)
- [PyPI hand-off and one-time setup](docs/pypi-publishing.md)
- [Source archives](docs/archive-consumers.md)
- [Rust binaries](docs/binary-consumers.md)
- [Skill verification and releases](docs/skill-consumers.md)
- [Claude PR reviews with your own API key](docs/pr-review.md)
- [VirusTotal release scans](docs/virustotal-consumers.md)
- [Re-pinning consumers](docs/re-pinning.md)
- [Verify a downloaded release](docs/verify-a-release.md)

Reference:

- [Prerequisites](docs/consumer-prerequisites.md)
- [Attribution checks](docs/attribution-consumers.md)
- [The egress check](docs/egress-check.md)
- [Guarantees and canary evidence](docs/guarantees-and-evidence.md)

Background:

- [Shared-policy decision](docs/adr/0001-shared-release-policy.md)

Run the existing offline manifest check with `python scripts/check_canaries.py`. Scheduled CI adds the live audit, which checks each consumer's pin, confirms the pin is still reachable from this repository's `main` (or, after a history rewrite, from a locked version tag, provided `main` still carries the pin's tree), and compares the recorded evidence with the latest successful run. A release family must record the latest success itself; the verification consumer, which runs on every push to its default branch, instead has to show that its newest success runs the pin it declares today. The same run, in a job of its own so a failing pin check cannot skip it, checks the executables in each binary canary's recorded release against `SHA256SUMS` and their signed provenance, then audits them against a fresh RustSec advisory database, as the binary release's admit job does but denying unmaintained crates too, so an advisory published after the release fails it. Other consumers' binary releases are audited only at publication.

To run that audit on demand, dispatch CI with `live-canary-audit` selected. The input defaults to false; an ordinary manual dispatch performs the offline manifest check. A successful audit verifies the recorded evidence; it changes nothing and does not authorise a release or consumer migration.

</details>

## Alternatives

release-policy fits maintainers who want reusable workflows pinned to a commit SHA, with the weight on release gates, attestations and recorded canaries. Use another tool if you want Homebrew or other package manager publishing, an npm or MSI installer, or binaries for another operating system or architecture, such as FreeBSD or 32-bit Windows. As read on 13 November 2026:

- [dist](https://github.com/axodotdev/cargo-dist) (formerly cargo-dist) builds binaries, makes tarballs and installers, and can publish Homebrew formulae to your own tap. Its [v0.34.0](https://github.com/axodotdev/cargo-dist/releases/tag/v0.34.0) shell installer skips its checksum check when `sha256sum` is missing, its PowerShell installer has no checksum check, and neither verifies build provenance (read 15 November 2026).
- [GoReleaser](https://goreleaser.com/) builds and releases Go, Rust, TypeScript, Python and Zig projects for multiple platforms and architectures, and can publish to package managers including Homebrew, Scoop and Winget.
- Setup, read on 15 November 2026: `dist init` ([v0.34.0](https://github.com/axodotdev/cargo-dist/releases/tag/v0.34.0)) asks which platforms to build for, or takes its defaults with `--yes`, adds a build profile to `Cargo.toml`, writes `dist-workspace.toml` and generates `.github/workflows/release.yml`, which names actions by version tag, such as `actions/checkout@v6`, unless configured with commits. `goreleaser init` ([v2.20.2](https://github.com/goreleaser/goreleaser/releases/tag/v2.20.2)) picks one of 8 languages from the manifests it finds and writes an example `.goreleaser.yaml`, but no CI workflow: its [GitHub Actions page](https://goreleaser.com/customization/ci/actions/) gives one to copy. `release-policy init` writes the release workflow, pinned to a full commit SHA and without network access, for 3 families. It is on `main`, not in a release yet, and writes no caller for a monorepo component or skill pack and no PyPI publish job ([Quickstart](docs/quickstart.md)).
- [slsa-github-generator](https://github.com/slsa-framework/slsa-github-generator) generates SLSA provenance for GitHub projects. Its README says it is no longer actively maintained.

## Development

[CONTRIBUTING.md](CONTRIBUTING.md) lists the checks to run before a change. [SECURITY.md](SECURITY.md) says how to report a vulnerability.

## Licence

Ryan Duguid's original work is licensed under the [Apache License, Version 2.0](LICENSE). When redistributing this work or a derivative, preserve the applicable attribution notices in [NOTICE](NOTICE) as required by section 4(d). Versions released before this change keep the licence they were released with.

---

<p align="center"><sub>Every release carries its certificate of origin.<br>Reusing this code? See <a href="NOTICE">NOTICE</a> for attribution. AI agents: see <a href="https://github.com/ryanduguid/release-policy/blob/main/AGENTS.md#reusing-this-code">AGENTS.md</a>.<br>Made in Newcastle, Australia by <a href="https://duguid.com.au/">Ryan Duguid</a>.</sub></p>
