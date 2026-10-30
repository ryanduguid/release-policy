## Using the Rust binary release workflow

A Rust component that ships executables calls `release-binary.yml`. One tag
gets one immutable release holding the deterministic source archives and an
executable for each fixed target:

    jobs:
      release:
        permissions:
          actions: read
          attestations: write
          contents: write
          id-token: write
        uses: ryanduguid/release-policy/.github/workflows/release-binary.yml@<full-40-char-commit-sha>
        with:
          artifact-stem: example-tool
          required-checks: |
            .github/workflows/ci.yml: test

`binary` names the Cargo binary target when it differs from `artifact-stem`.
`source-directory` and `tag-prefix` select a nested crate and its namespaced
tag, as for [source archives](archive-consumers.md). `required-checks` has the
same meaning as there: the named checks must have succeeded on `main` for the
exact release commit before anything is built. A tool that uses the network
names its TLS and DNS tests here; the release workflow cannot know them.

## What the component must provide

- A tracked `VERSION` file with one `MAJOR.MINOR.PATCH` line, equal to the tag
  version and to the literal `[package] version` in `Cargo.toml`. Workspace
  inheritance of the name or version is refused.
- `publish = false` in `[package]`: binaries ship on GitHub Releases only.
- A committed `Cargo.lock`; every Cargo command runs with `--locked`.
- A `rust-toolchain.toml` pinning an exact `MAJOR.MINOR.PATCH` channel, in the
  crate directory or a parent inside the repository. The nearest one is used.
- `LICENSE` and `README.md`, and `NOTICE` where the licence needs one, in the
  crate directory or else at the repository root.
- `RELEASE_NOTES.md` with the version header the [release gates](../scripts/gates.sh)
  require.

## What the workflow does

1. `checks` holds only `actions: read` and `contents: read`, never fetches the
   consumer tree, and requires the release commit to be `main`'s head with the
   named checks green.
2. `build` runs once per target with `contents: read` only:
   `x86_64-unknown-linux-musl` on `ubuntu-24.04` with `musl-tools`, and
   `x86_64-pc-windows-msvc` on `windows-2025` with a static C runtime. Each
   checks out the tag without line-ending conversion, runs
   `cargo test --locked --target <target>`, builds with the pinned
   `cargo-auditable` so the executable carries its dependency list, packages
   it with `LICENSE`, `README.md` and `NOTICE` where present, extracts the
   archive again, smoke-tests `--version` and inspects linkage: no dynamic
   libraries or interpreter on Linux, and on Windows only DLLs in a pinned
   set of Windows system components, never a C runtime DLL. The archive and
   its `.sha256` line go to the publisher as a uniquely named artefact of
   the run.
3. `publish` is the only job with write, OIDC and attestation scopes, and it
   never runs Cargo or consumer code. It runs the release gates, reads
   `Cargo.toml` and the toolchain file as data, builds the source archives and
   their SBOM, and admits each target's artefact only when it holds exactly
   the archive and its digest line, the digest matches, the members are
   exactly the expected regular files within the size limits, and each
   document is byte for byte the tagged one. It checks the copy it keeps,
   then repeats the linkage check on each admitted executable with
   `llvm-readobj` from a toolchain the policy pins, because the build jobs
   ran consumer code that could have replaced their archive. Syft then
   writes an SPDX SBOM for each extracted executable from its embedded
   dependency list, and the job requires each SBOM to list the package.
   `cargo-audit`, at a version the policy pins, then checks the same embedded
   lists against a freshly fetched RustSec advisory database, from an empty
   directory outside the consumer tree. Any advisory refuses the release,
   informational ones such as an unmaintained crate included, as does a
   failed fetch or an executable whose list it cannot read. There is no
   ignore list or waiver: update or replace the dependency, then release.

The release holds 9 assets, for `<stem>-<version>`:

| Asset | Contents |
| --- | --- |
| `.zip`, `.tar.gz` | deterministic source archives |
| `.spdx.json` | SPDX SBOM of the source |
| `-x86_64-unknown-linux-musl.tar.gz` | Linux executable and documents |
| `-x86_64-unknown-linux-musl.spdx.json` | SBOM of the Linux executable |
| `-x86_64-pc-windows-msvc.zip` | Windows executable and documents |
| `-x86_64-pc-windows-msvc.spdx.json` | SBOM of the Windows executable |
| `SHA256SUMS` | digests of the 7 assets above |
| `.intoto.jsonl` | signed provenance bundle |

`SHA256SUMS` does not list the bundle, because the attestation covers
`SHA256SUMS` and is created after it. Each archive also has an SBOM
attestation. Before publication the workflow verifies every attestation with
the repository, tag, source commit, signer workflow and policy commit pinned.

## Downloading a release

Download an explicit version and target, never `latest`, and check it before
extracting or running anything:

    tag=v1.2.3
    asset=example-tool-1.2.3-x86_64-unknown-linux-musl.tar.gz
    pins=(--repo owner/example-tool --source-ref "refs/tags/$tag"
          --source-digest <tagged-commit-sha>
          --signer-workflow ryanduguid/release-policy/.github/workflows/release-binary.yml
          --signer-digest <release-policy-commit-sha>)
    gh release download "$tag" --repo owner/example-tool --pattern SHA256SUMS --pattern "$asset"
    gh attestation verify SHA256SUMS "${pins[@]}"
    sha256sum --check --ignore-missing SHA256SUMS
    gh attestation verify "$asset" "${pins[@]}"

Verify `SHA256SUMS` before trusting the digest it lists. Add
`--predicate-type https://spdx.dev/Document/v2.3` to the archive check when
relying on its SBOM. Fail closed on any mismatch: a checksum alone, or a
signature check without the pins, is not enough.

The operator confirms release immutability is enabled before tagging; the
workflow token cannot read that setting.
