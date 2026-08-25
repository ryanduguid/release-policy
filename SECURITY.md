# Security policy

## Supported versions

Security fixes target the latest commit on the default branch. Older commits
and historical tags receive fixes only through an update to the current
default branch. Consumers that pin a reviewed commit SHA must update that pin
to receive a fix. The notes-only `v0.1.0` marker is not a separately maintained
release line.

## Reporting a vulnerability

Use GitHub's private **Report a vulnerability** form when it is available in
this repository's Security tab. If the form is absent, do not disclose the
vulnerability in a public issue or public discussion. Include a clear
description, safe reproduction steps, impact, and any suggested mitigation.

## Safe reporting data

Use fabricated or redacted evidence only. Do not include real client data,
credentials, access tokens, cookies, secrets, `.env` contents, private keys,
workpapers, or release credentials.

A security report does not authorise anyone to run a release, create a tag,
publish an asset, use consumer credentials, or disclose the concern publicly.

## Supply-chain note

This repository publishes reusable GitHub Actions workflows that other
projects pin by commit SHA. A compromised workflow can mint attestations or
publish artefacts in every consumer. Treat findings here as high severity
even when the shell scripts themselves look small.

The workflows mint GitHub attestations and read the GitHub API. They do not
hold package-registry tokens; consumers that publish to PyPI do so with
OIDC trusted publishing in the caller.

The verifier jobs execute reviewed consumer code with only `contents: read`.
They declare no secrets or outputs, use no cache and pass no artefacts to
publication. `publish-archives.yml` is the privileged core and receives exactly
`attestations: write`, `contents: write` and `id-token: write`. It starts with
fresh consumer and policy checkouts in sibling directories and never executes
consumer tests.
