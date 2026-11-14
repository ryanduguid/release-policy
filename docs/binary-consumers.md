## Using the Rust binary release workflow

A Rust component that ships executables calls `release-binary.yml`. One tag
gets one immutable release holding the deterministic source archives and an
executable for each fixed target (`.github/workflows/release.yml` in the
consumer):

    name: Release
    on:
      push:
        tags: ["v*"]
    permissions:
      contents: read
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

Replace `<full-40-char-commit-sha>` with a commit you have reviewed. This
prints the commit of this repository's latest release:

    gh api repos/ryanduguid/release-policy/commits/$(gh api repos/ryanduguid/release-policy/releases/latest --jq .tag_name) --jq .sha

Release v0.3.0 predates the Linux arm64 and macOS arm64 targets, `data-files`
and several names in `binary`; use those only with v0.4.0's commit or a later
one.

`binary` names the Cargo binary target when it differs from `artifact-stem`,
or several targets separated by spaces; each archive ships all of them.
`data-files` lists files the executables need at run time, such as a CA
bundle, one path per line relative to the crate directory:

        with:
          artifact-stem: example-tool
          binary: example-tool example-tool-helper
          data-files: |
            certs/ca-bundle.pem

Each ships beside the executables under the last part of its path, so
`certs/ca-bundle.pem` becomes `ca-bundle.pem`. It must be a tracked regular
file inside the crate directory, reached through no symbolic link, and its
name must not repeat another member's or a document's.
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
- Code and tests that build and pass on each target below, and an
  executable that answers `--version` with a first line ending in the
  version, for each binary shipped.
- A committed `Cargo.lock`; every Cargo command runs with `--locked`.
- A `rust-toolchain.toml` pinning an exact `MAJOR.MINOR.PATCH` channel, in the
  crate directory or a parent inside the repository. The nearest one is used.
- `LICENSE` and `README.md`, and `NOTICE` where the licence needs one, in the
  crate directory or else at the repository root.
- `RELEASE_NOTES.md` with the version header the [release gates](../scripts/gates.sh)
  require.

## Recording the released commit

The build step sets `RELEASE_SOURCE_COMMIT` to the released commit, which the
`checks` job required to be `main`'s head, and refuses to build unless it is
40 lowercase hexadecimal characters. `cargo test` and the build both see it, so
a crate can record it at compile time:

    const SOURCE_COMMIT: &str = match option_env!("RELEASE_SOURCE_COMMIT") {
        Some(commit) => commit,
        None => "unknown",
    };

Builds outside this workflow, and callers pinned to a policy commit from
before this change, leave it unset.

## What the workflow does

1. `checks` holds only `actions: read` and `contents: read`, never fetches the
   consumer tree, and requires the release commit to be `main`'s head with the
   named checks green.
2. `build` runs once per target with `contents: read` only, each natively
   on its own runner: `x86_64-unknown-linux-musl` on `ubuntu-24.04` and
   `aarch64-unknown-linux-musl` on `ubuntu-24.04-arm`, both with
   `musl-tools`; `aarch64-apple-darwin` on `macos-15`; and
   `x86_64-pc-windows-msvc` on `windows-2025` with a static C runtime. Each
   checks out the tag without line-ending conversion, runs
   `cargo test --locked --target <target>`, builds with the pinned
   `cargo-auditable` so each executable carries its dependency list, packages
   the executables with the data files and with `LICENSE`, `README.md` and
   `NOTICE` where present, extracts the archive again, smoke-tests each
   executable's `--version` and inspects its linkage. On Linux it may load no
   dynamic library and name no interpreter. On macOS, which has no static
   executables, it must be a thin arm64 executable file, never a universal
   one, that loads only libraries macOS ships, by absolute path: one directly
   in `/usr/lib` or a framework in `/System/Library/Frameworks`, never one
   through `@rpath`, `@executable_path` or `@loader_path`, or from Homebrew or
   `/usr/local`. Its load commands are read directly, so every kind of library
   load counts, and one that adds a run-path search path, sets dyld
   environment variables or names a dynamic linker other than `/usr/lib/dyld`
   is refused. On
   Windows it may import only DLLs in a pinned set of Windows system
   components, never a C runtime DLL. The archive and its `.sha256` line go
   to `admit` and the publisher as a uniquely named artefact of the run.
   Intel macOS (`x86_64-apple-darwin`) is not built: on the arm64 runner its
   tests and smoke test would run under Rosetta rather than natively, and a
   native Intel runner would be a fifth build job.
3. `admit` holds `contents: read` only. It runs the steps that rely on tools
   or data fetched at run time (Syft, a rustup toolchain and `cargo-audit`
   with its advisory database), so none of them runs with a write, OIDC or
   attestation scope. It builds the source archives and their SBOM, and
   admits each target's artefact only when it holds exactly the archive and
   its digest line, the digest matches, the members are exactly the expected
   regular files within the size limits, and each document and data file is
   byte for byte the tagged one. It checks the copy it keeps, then repeats
   the linkage check on each admitted executable with `llvm-readobj` from a
   toolchain the policy pins, because the build jobs ran consumer code that
   could have replaced their archive. Syft then writes an SPDX SBOM for each
   target from the dependency lists embedded in its extracted executables,
   and the job requires each SBOM to list the package. `cargo-audit`, at a version the policy pins,
   then checks the same embedded lists against a freshly fetched RustSec
   advisory database, from an empty directory outside the consumer tree. Any
   advisory refuses the release, informational ones such as an unmaintained
   crate included, as does a failed fetch or an executable whose list it
   cannot read. There is no ignore list or waiver: update or replace the
   dependency, then release. The job hands the 6 archives and 5 SBOMs to the
   publisher as one artefact, with each file's SHA-256 as a job output.
4. `publish` is the only job with write, OIDC and attestation scopes. It
   never runs Cargo, consumer code or the admit job's third-party tools. It
   runs the release gates, reads `Cargo.toml` and the toolchain file as data,
   builds the source archives again and admits each target's artefact again
   by the same rules. The admit job's artefact must then hold exactly the 11
   expected files, each matching its digest output and each archive byte for
   byte the publisher's own, so an archive changed together with its digest
   is refused. The publisher keeps only the SBOMs from it and requires each
   binary SBOM to list the package. Without those tools it cannot make the
   SBOMs or repeat the linkage check and the audit, so for these it relies on
   the admit job.

The release holds 13 assets, for `<stem>-<version>`:

| Asset | Contents |
| --- | --- |
| `.zip`, `.tar.gz` | deterministic source archives |
| `.spdx.json` | SPDX SBOM of the source |
| `-x86_64-unknown-linux-musl.tar.gz` | Linux x86_64 executables, data files and documents |
| `-x86_64-unknown-linux-musl.spdx.json` | SBOM of the Linux x86_64 executables |
| `-aarch64-unknown-linux-musl.tar.gz` | Linux arm64 executables, data files and documents |
| `-aarch64-unknown-linux-musl.spdx.json` | SBOM of the Linux arm64 executables |
| `-aarch64-apple-darwin.tar.gz` | macOS arm64 executables, data files and documents |
| `-aarch64-apple-darwin.spdx.json` | SBOM of the macOS arm64 executables |
| `-x86_64-pc-windows-msvc.zip` | Windows x86_64 executables, data files and documents |
| `-x86_64-pc-windows-msvc.spdx.json` | SBOM of the Windows x86_64 executables |
| `SHA256SUMS` | digests of the 11 assets above |
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

## Installing a release

`scripts/install.sh` (Linux and macOS) and `scripts/install.ps1` (Windows)
download, verify and install one release in a single command. Each takes the
repository, the full tag and, optionally, the directory to install into. On
Linux or macOS:

    curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/ryanduguid/release-policy/<full-40-char-commit-sha>/scripts/install.sh | sh -s -- owner/example-tool v1.2.3

In PowerShell on Windows:

    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ryanduguid/release-policy/<full-40-char-commit-sha>/scripts/install.ps1))) owner/example-tool v1.2.3

Replace `<full-40-char-commit-sha>` with a commit of this repository that
holds the installers, and read the script at that commit first: the command
runs it unchecked, trusting GitHub and HTTPS to serve that commit's file. A
commit cannot change; a branch such as `main` would run whatever the branch
holds when you run the command. Each installer:

- picks the archive for the running platform: Linux x86_64 or arm64 or macOS
  arm64 through `install.sh`, Windows x86_64 through `install.ps1`. It refuses
  any other platform, Intel macOS and Windows on Arm included.
- downloads `SHA256SUMS`, that archive and the provenance bundle from the
  release, and refuses on any failed or partial download, or unless
  `SHA256SUMS` lists exactly one archive for the platform.
- verifies `SHA256SUMS` and the archive against the bundle with
  `gh attestation verify`, pinned to the repository, `refs/tags/<tag>`,
  this repository's `release-binary.yml` as the signer workflow and a
  GitHub-hosted runner, then requires the archive to match its `SHA256SUMS`
  line.
- installs the archive's files, each of which must be a plain file at its top
  level, into the directory: by default `${XDG_DATA_HOME:-$HOME/.local/share}/<stem>`
  or `%LOCALAPPDATA%\Programs\<stem>`. A file of the same name is replaced;
  other files stay.

Nothing is installed unless every check passes. `install.sh` needs `curl`,
`tar` and `sha256sum` or `shasum`; `install.ps1` runs in Windows PowerShell
5.1 or PowerShell 7. Both need the [GitHub CLI](https://cli.github.com/), with
no sign-in, because the bundle comes from the release. Neither changes `PATH`:
each prints the directory to add.

Unlike the manual check above, the installers do not pin the tagged commit or
the policy commit. They establish that a GitHub-hosted run of
`release-binary.yml`, at some commit of this repository, attested the archive
and `SHA256SUMS` for that repository's tag. They do not establish which commit the tag named, nor
that the run used a policy commit you reviewed, so they trust whoever can push
tags and change workflows in that repository. Use the manual check with both
digests pinned when that matters.
