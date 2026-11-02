<p align="center">
  <picture>
    <source media="(max-width: 600px) and (prefers-color-scheme: light)" srcset=".github/assets/banner-phone-light.svg">
    <source media="(max-width: 600px)" srcset=".github/assets/banner-phone.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/banner-light.svg">
    <img src=".github/assets/banner.svg" width="100%" alt="Release Policy. Releases with provenance, by reusable workflow. Pixel art of a wax seal with a star and ribbons beside the Release Policy wordmark in slab-serif block letters.">
  </picture>
</p>

<div align="center">

[![tests](https://github.com/ryanduguid/release-policy/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ryanduguid/release-policy/actions/workflows/ci.yml) [![Codecov](https://codecov.io/gh/ryanduguid/release-policy/graph/badge.svg)](https://codecov.io/gh/ryanduguid/release-policy) [![CodeQL](https://github.com/ryanduguid/release-policy/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/ryanduguid/release-policy/actions/workflows/codeql.yml) [![SonarCloud](https://sonarcloud.io/api/project_badges/measure?project=ryanduguid_release-policy&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=ryanduguid_release-policy) [![Codacy code quality](https://app.codacy.com/project/badge/Grade/49440ac0ee144a12b5fc1380425e9773)](https://app.codacy.com/gh/ryanduguid/release-policy/dashboard)<br> [![OpenSSF Scorecard](https://raw.githubusercontent.com/ryanduguid/.github/badges/ryanduguid/release-policy.svg)](https://scorecard.dev/viewer/?uri=github.com/ryanduguid/release-policy) [![OpenSSF Best Practices](https://www.bestpractices.dev/projects/15422/badge)](https://www.bestpractices.dev/projects/15422)<br> [![release](https://badgen.net/github/release/ryanduguid/release-policy/stable?label=release&color=66023C&labelColor=000000)](https://github.com/ryanduguid/release-policy/releases/latest) [![licence: FSL-1.1-ALv2](.github/assets/badges/licence.svg)](LICENSE)

[Prerequisites](docs/consumer-prerequisites.md) · [Verify a release](docs/verify-a-release.md) · [Guarantees](docs/guarantees-and-evidence.md) · [Canaries](canaries.json) · [Licence](#licence)

</div>

Reusable GitHub Actions workflows that release a repository's Python packages, source archives, Rust binaries and skill packs with checksums and SLSA provenance, plus workflows for VirusTotal release scans, attribution checks and independent PR reviews. A consumer calls one with `uses:`, pinned to a full commit SHA of this repository.

| Workflow | Use it to | Guide |
| --- | --- | --- |
| `release-python.yml` | Build, attest and hand a Python package to PyPI | [Packaged Python](docs/python-consumers.md) |
| `release-archive.yml` | Publish a checksummed, attested source archive | [Source archives](docs/archive-consumers.md) |
| `release-binary.yml` | Build and publish Linux and Windows Rust executables | [Rust binaries](docs/binary-consumers.md) |
| `release-skills.yml`, `verify-skills.yml` | Verify and release an agent skill pack | [Skill verification and releases](docs/skill-consumers.md) |
| `virustotal-release.yml` | Scan a release's assets with VirusTotal | [VirusTotal release scans](docs/virustotal-consumers.md) |
| `attribution-policy.yml` | Fail PR text and commits that carry AI attribution credits | [Attribution checks](docs/attribution-consumers.md) |
| `pr-review.yml` | Run an independent PR review with your own API key | [PR reviews](docs/pr-review.md) |

Policy checks establish release evidence, not accounting correctness.

**Input:** the recorded `v0.4.0` Python canary from AU FP&A Pack.

The consumer's release caller at that tag is:

```yaml
uses: ryanduguid/release-policy/.github/workflows/release-python.yml@0f7ca4ce0ba2e7ea52fc611149a42cd5d0a26f6c
```

**Recorded output:** run 40612457692 on 28 October 2026 supplies the release evidence for [au-fpa-pack v0.4.0](https://github.com/ryanduguid/au-fpa-pack/releases/tag/v0.4.0): the wheel, source archive, SBOM, provenance, release manifest and `SHA256SUMS`, with policy SHA `0f7ca4ce0ba2e7ea52fc611149a42cd5d0a26f6c`.

**Human decision:** Check the release artefacts and their attestations against that recorded policy revision. This evidence applies to the recorded policy revision. Later policy changes need their own verification.

This snapshot was checked against [canaries.json](canaries.json) on 2 November 2026. The manifest retains the source commit, policy SHA and run identity; consult it for later updates. This repository has no package installation command.

Start with the [consumer prerequisites](docs/consumer-prerequisites.md) before choosing a release workflow.

<details>
<summary>Consumer setup, policy guarantees and historical limits</summary>

## Release status

[`v0.3.0`](https://github.com/ryanduguid/release-policy/releases/tag/v0.3.0)
(28 October 2026) is the first release with an artefact: the `release-policy`
executable for Linux and Windows with SBOMs, provenance attestations and
`SHA256SUMS`, built and published by this repository's own `release-binary.yml`.
The workflows still run the Python scripts, not that executable. It is the first
FSL-1.1-ALv2 release; earlier releases keep their own licence.

`v0.1.0` and `v0.2.0` are notes-only source markers with no uploaded artefact.
`v0.2.0` marks `a601f1d8b143f92b0517f709216993660d21f480`, the revision ryanduguid
source repositories pinned on 19 October 2026. GitHub marks the releases
immutable, so their tags and assets are fixed; the markers must not be
retrofitted with assets or described as distributables.

Each consumer migration remains a separate reviewed change pinned to a full
policy commit.

## Consumer guides

- [Packaged Python](docs/python-consumers.md)
- [PyPI hand-off and one-time setup](docs/pypi-publishing.md)
- [Source archives](docs/archive-consumers.md)
- [Rust binaries](docs/binary-consumers.md)
- [Skill verification and releases](docs/skill-consumers.md)
- [Attribution checks](docs/attribution-consumers.md)
- [The egress check](docs/egress-check.md)
- [Independent PR reviews with your own API key](docs/pr-review.md)
- [VirusTotal release scans](docs/virustotal-consumers.md)
- [Prerequisites](docs/consumer-prerequisites.md)
- [Re-pinning consumers](docs/re-pinning.md)
- [Guarantees and canary evidence](docs/guarantees-and-evidence.md)
- [Verify a downloaded release](docs/verify-a-release.md)
- [Shared-policy decision](docs/adr/0001-shared-release-policy.md)

Run the existing offline manifest check with `python scripts/check_canaries.py`. Scheduled CI adds the live audit, which checks each consumer's pin, confirms the pin is still reachable from this repository's `main`, and compares the recorded evidence with the latest successful run. A release family must record the latest success itself; the verification consumer, which runs on every push to its default branch, instead has to show that its newest success runs the pin it declares today. The same run audits the executables in this repository's latest release against a fresh RustSec advisory database, as the binary publisher does, so an advisory published after the release fails it. Consumers' binary releases are audited only at publication.

To run that audit on demand, dispatch CI with `live-canary-audit` selected. The input defaults to false; an ordinary manual dispatch performs the offline manifest check. A successful audit verifies the recorded evidence; it changes nothing and does not authorise a release or consumer migration.

</details>

## Licence

Licensed under the Functional Source License, Version 1.1, ALv2 Future License (FSL-1.1-ALv2). Each version becomes available under the Apache License, Version 2.0, on the second anniversary of its release, as [LICENSE](LICENSE) sets out.

---

<p align="center"><sub>Every release carries its certificate of origin.<br>Made in Newcastle, Australia by <a href="https://duguid.com.au/">Ryan Duguid</a>.</sub></p>
