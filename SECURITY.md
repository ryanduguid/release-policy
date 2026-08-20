# Security policy

## Supported versions

Security fixes are applied to the latest version on the default branch.

## Reporting a vulnerability

Please use this repository's private vulnerability reporting feature. Do not
open a public issue for a suspected security vulnerability. Include a clear
description, reproduction steps, impact, and any suggested mitigation.

A valid report will be acknowledged within seven days, and the fix and
disclosure timeline will be agreed with the reporter.

## Supply-chain note

This repository publishes reusable GitHub Actions workflows that other
projects pin by commit SHA. A compromised workflow can mint attestations or
publish artefacts in every consumer. Treat findings here as high severity
even when the shell scripts themselves look small.

The workflows mint GitHub attestations and read the GitHub API. They do not
hold package-registry tokens; consumers that publish to PyPI do so with
OIDC trusted publishing in the caller.
