# release-policy: trace a release to its checked artefacts

Policy checks establish release evidence, not accounting correctness.

**Input:** the recorded `review-ready-gate/v0.1.3` Python canary from Accounting Review Pipeline.

The consumer's approved integration line is:

```yaml
uses: ryanduguid/release-policy/.github/workflows/release-python.yml@787db4590e725cfd37104c8a9dd9e75f7fd4c018
```

**Recorded output:** [run 33671339947](https://github.com/ryanduguid/accounting-review-pipeline/actions/runs/33671339947) supplies the release evidence for [review-ready-gate/v0.1.3](https://github.com/ryanduguid/accounting-review-pipeline/releases/tag/review-ready-gate/v0.1.3), with policy SHA `3ff09b654a17b9a3b55548e25e6108ee582b00c4`.

**Human decision:** Check the release artefacts and their attestations against that recorded policy revision. The current consumer pin is newer than this release evidence; the earlier run does not certify the newer pin.

This example is read from [canaries.json](canaries.json), which retains the source commit, policy SHA and run identity. This repository has no package installation command.

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

Run the existing offline manifest check with `python scripts/check_canaries.py`. Scheduled CI adds the live pin and successful-run audit.

Consumer migrations remain separately reviewed changes pinned to full policy commits. No workflow or repository protection changes accompany this documentation.

</details>
