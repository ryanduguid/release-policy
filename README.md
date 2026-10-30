<p align="center">
  <picture>
    <source media="(max-width: 600px) and (prefers-color-scheme: light)" srcset=".github/assets/banner-phone-light.svg">
    <source media="(max-width: 600px)" srcset=".github/assets/banner-phone.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/banner-light.svg">
    <img src=".github/assets/banner.svg" width="100%" alt="Release Policy. Releases with provenance, by reusable workflow. Pixel art of a wax seal with a star and ribbons beside the Release Policy wordmark in slab-serif block letters.">
  </picture>
</p>

<div align="center">

[![tests](https://img.shields.io/github/actions/workflow/status/ryanduguid/release-policy/ci.yml?branch=main&label=tests&labelColor=000000&style=flat)](https://github.com/ryanduguid/release-policy/actions/workflows/ci.yml) [![Codecov](https://img.shields.io/codecov/c/github/ryanduguid/release-policy?label=coverage&labelColor=000000&style=flat)](https://codecov.io/gh/ryanduguid/release-policy) [![CodeQL](https://img.shields.io/github/actions/workflow/status/ryanduguid/release-policy/codeql.yml?branch=main&label=CodeQL&labelColor=000000&style=flat)](https://github.com/ryanduguid/release-policy/actions/workflows/codeql.yml) [![Snyk](https://img.shields.io/github/actions/workflow/status/ryanduguid/release-policy/snyk.yml?branch=main&label=Snyk&labelColor=000000&style=flat)](https://github.com/ryanduguid/release-policy/actions/workflows/snyk.yml) [![SonarCloud](https://img.shields.io/sonar/quality_gate/ryanduguid_release-policy?server=https%3A%2F%2Fsonarcloud.io&label=SonarCloud&labelColor=000000&style=flat)](https://sonarcloud.io/summary/new_code?id=ryanduguid_release-policy) [![Codacy code quality](https://img.shields.io/codacy/grade/49440ac0ee144a12b5fc1380425e9773?label=Codacy&labelColor=000000&style=flat)](https://app.codacy.com/gh/ryanduguid/release-policy/dashboard)<br> [![OpenSSF Scorecard](https://img.shields.io/ossf-scorecard/github.com/ryanduguid/release-policy?label=Scorecard&labelColor=000000&style=flat)](https://scorecard.dev/viewer/?uri=github.com/ryanduguid/release-policy) [![OpenSSF Best Practices](https://img.shields.io/cii/level/15422?label=Best%20Practices&labelColor=000000&style=flat)](https://www.bestpractices.dev/projects/15422)<br> [![release](https://img.shields.io/github/v/release/ryanduguid/release-policy?color=66023C&labelColor=000000&style=flat)](https://github.com/ryanduguid/release-policy/releases/latest) [![licence: FSL-1.1-ALv2](https://img.shields.io/badge/licence-FSL--1.1--ALv2-66023C?labelColor=000000&style=flat&color=66023C)](LICENSE)

</div>

Policy checks establish release evidence, not accounting correctness.

**Input:** the recorded `review-ready-gate/v0.1.5` Python canary from Accounting Review Pipeline.

The consumer's recorded integration line on 10 September 2026 is:

```yaml
uses: ryanduguid/release-policy/.github/workflows/release-python.yml@fcf25e532e9eb60056ae6e5c819cf3125c4f4b91
```

**Recorded output:** run 34163202701 supplies the release evidence for [review-ready-gate/v0.1.5](https://github.com/ryanduguid/accounting-review-pipeline/releases/tag/review-ready-gate/v0.1.5), with policy SHA `fcf25e532e9eb60056ae6e5c819cf3125c4f4b91`.

**Human decision:** Check the release artefacts and their attestations against that recorded policy revision. This evidence applies to the recorded policy revision. Later policy changes need their own verification.

This snapshot was checked against [canaries.json](canaries.json) on 10 September 2026. The manifest retains the source commit, policy SHA and run identity; consult it for later updates. This repository has no package installation command.

Start with the [consumer prerequisites](docs/consumer-prerequisites.md) before choosing a release workflow.

<details>
<summary>Consumer setup, policy guarantees and historical limits</summary>

## Release status

`v0.1.0` is a manually created, notes-only historical source marker. It has no
uploaded, checksummed module artefact and is not a verified distributable
module release. GitHub marks the release immutable, so its tag and assets are
fixed; only its title and notes can still be edited, and it must not be
retrofitted with assets or described as a distributable.

The repository now contains reviewed-in-code source-archive and initial
skill-pack workflow implementations. That does not retrofit `v0.1.0`, make the
module itself distributable, or prove either phase through this repository's
own tag. Each consumer migration remains a separate reviewed change pinned to
a full policy commit.

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
- [Shared-policy decision](docs/adr/0001-shared-release-policy.md)

Run the existing offline manifest check with `python scripts/check_canaries.py`. Scheduled CI adds the live audit, which checks each consumer's pin, confirms the pin is still reachable from this repository's `main`, and compares the recorded evidence with the latest successful run. A release family must record the latest success itself; the verification consumer, which runs on every push to its default branch, instead has to show that its newest success runs the pin it declares today. The same run audits the executables in this repository's latest release against a fresh RustSec advisory database, as the binary publisher does, so an advisory published after the release fails it. Consumers' binary releases are audited only at publication.

To run that audit on demand, dispatch CI with `live-canary-audit` selected. The input defaults to false; an ordinary manual dispatch performs the offline manifest check. A successful audit verifies the recorded evidence; it changes nothing and does not authorise a release or consumer migration.

</details>

---

<p align="center"><sub>Every release carries its certificate of origin.<br>Made in Newcastle, Australia by <a href="https://duguid.com.au/">Ryan Duguid</a>.</sub></p>
