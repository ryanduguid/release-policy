# release-policy: trace a release to its checked artefacts

Policy checks establish release evidence, not accounting correctness.

**Input:** the recorded `review-ready-gate/v0.1.5` Python canary from Accounting Review Pipeline.

The consumer's recorded integration line on 10 September 2026 is:

```yaml
uses: ryanduguid/release-policy/.github/workflows/release-python.yml@fcf25e532e9eb60056ae6e5c819cf3125c4f4b91
```

**Recorded output:** [run 34163202701](https://github.com/ryanduguid/accounting-review-pipeline/actions/runs/34163202701) supplies the release evidence for [review-ready-gate/v0.1.5](https://github.com/ryanduguid/accounting-review-pipeline/releases/tag/review-ready-gate/v0.1.5), with policy SHA `fcf25e532e9eb60056ae6e5c819cf3125c4f4b91`.

**Human decision:** Check the release artefacts and their attestations against that recorded policy revision. This evidence applies to the recorded policy revision. Later policy changes need their own verification.

This snapshot was checked against [canaries.json](canaries.json) on 10 September 2026. The manifest retains the source commit, policy SHA and run identity; consult it for later updates. This repository has no package installation command.

<details>
<summary>Consumer setup, policy guarantees and historical limits</summary>

## Release status

`v0.1.0` is a manually created, notes-only historical source marker. It has no
uploaded, checksummed module artefact and is not a verified distributable
module release. It remains mutable, but must not be silently rewritten,
replaced, or retrofitted with assets.

The repository now contains reviewed-in-code source-archive and initial
skill-pack workflow implementations. That does not retrofit `v0.1.0`, make the
module itself distributable, or prove either phase through this repository's
own tag. Each consumer migration remains a separate reviewed change pinned to
a full policy commit.

## Consumer guides

- [Packaged Python](docs/python-consumers.md)
- [PyPI hand-off and one-time setup](docs/pypi-publishing.md)
- [Source archives](docs/archive-consumers.md)
- [Skill verification and releases](docs/skill-consumers.md)
- [Prerequisites](docs/consumer-prerequisites.md)
- [Guarantees and canary evidence](docs/guarantees-and-evidence.md)
- [Shared-policy decision](docs/adr/0001-shared-release-policy.md)

Run the existing offline manifest check with `python scripts/check_canaries.py`. Scheduled CI adds the live audit, which checks each consumer's pin, confirms the pin is still reachable from this repository's `main`, and compares the recorded evidence with the latest successful run.

</details>
