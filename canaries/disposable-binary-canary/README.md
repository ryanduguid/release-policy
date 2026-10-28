# disposable-binary-canary

A throwaway executable for testing `release-binary.yml` end to end. Its
releases are deleted once checked; nothing should download or run it.

Tagging `disposable-binary-canary/v<VERSION>` on `main`'s head runs
`.github/workflows/release-disposable-binary-canary.yml`, which builds, attests
and publishes it through the binary family. That workflow accepts one exact
tag. An immutable release's tag cannot be reused after its release is deleted,
so a new run needs a reviewed change raising `VERSION`, the `Cargo.toml`
version (then `cargo generate-lockfile`), the notes header and the workflow's
tag together.
