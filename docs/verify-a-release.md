# Verify a release

A repository that releases through these workflows attaches signed build
provenance to every release; some show an 'SLSA 3' badge, which
[SLSA Build Level](guarantees-and-evidence.md#slsa-build-level) explains. These
steps check a downloaded asset with a current
[GitHub CLI](https://cli.github.com/). They
prove where and how the asset was built, not that its contents are correct.

The commands assume a POSIX shell with GNU `sha256sum`, such as Linux, WSL or
Git Bash. Elsewhere, put each `gh` command on one line without the trailing
`\`; step 4 checks each asset's digest without `sha256sum`.

## 1. Download the asset, its checksums and its provenance

From the release page, or with the CLI:

```bash
gh release download <tag> --repo <owner>/<repo> \
  --pattern '<asset>' --pattern SHA256SUMS --pattern '*.intoto.jsonl'
```

`<tag>` is the full tag, including any component prefix, such as
`aus-accounting-mcp/v0.7.1`.

## 2. Check the checksum

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

The provenance bundle is not listed in `SHA256SUMS`; the attestation it holds
covers the files that are.

## 3. Verify the provenance

The signing workflow lives in `ryanduguid/release-policy`, not in the
repository that published the release, so `--repo` alone fails. Name the
signer as well:

```bash
gh attestation verify <asset> --repo <owner>/<repo> \
  --signer-repo ryanduguid/release-policy
```

That reads the attestation from GitHub's API. To verify against the
downloaded bundle instead, add `--bundle <stem>-<version>.intoto.jsonl`.

For a stricter check, replace `--signer-repo` with the exact signer workflow
and the tag the release was built from:

```bash
gh attestation verify <asset> --repo <owner>/<repo> \
  --signer-workflow ryanduguid/release-policy/.github/workflows/<signer>.yml \
  --source-ref refs/tags/<tag>
```

| The repository calls | `<signer>` |
| --- | --- |
| `release-archive.yml` or `release-skills.yml` | `publish-archives` |
| `release-python.yml` | `release-python` |
| `release-binary.yml` | `release-binary` |

## 4. Check the immutable release

The release and its assets are immutable once published. To confirm that the
asset you hold is the one the release published:

```bash
gh release verify <tag> --repo <owner>/<repo>
gh release verify-asset <tag> <asset> --repo <owner>/<repo>
```

[Guarantees and canary evidence](guarantees-and-evidence.md) describes what the
workflows check before publication.
