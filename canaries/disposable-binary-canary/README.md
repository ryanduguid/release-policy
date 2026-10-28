# disposable-binary-canary

A throwaway executable for testing `release-binary.yml` end to end. Its
releases are deleted once checked; nothing should download or run it.

Tagging `disposable-binary-canary/v<VERSION>` on `main`'s head runs
`.github/workflows/release-disposable-binary-canary.yml`, which builds, attests
and publishes it through the binary family. Raise `VERSION`, the
`Cargo.toml` version and the notes header together for each new run, because
an immutable release's tag cannot be reused after its release is deleted.
