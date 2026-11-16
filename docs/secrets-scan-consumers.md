# Secrets scan

The composite action `.github/actions/secrets-scan` is the whole body of a repository's
secrets job. It checks out the full history, installs a pinned
[betterleaks](https://github.com/betterleaks/betterleaks) release and scans commits for
credentials and for client identifiers: a tax file number, ABN, bank detail or Medicare
number beside its label.

## What a caller commits

1. A job whose only step is the action, pinned to the commit of a release tag here:

   ```yaml
   on:
     workflow_dispatch:
     pull_request:
     schedule:
       - cron: "29 5 * * 2"
     push:
       branches:
         - main

   permissions:
     contents: read

   jobs:
     secrets:
       name: secrets
       timeout-minutes: 10
       runs-on: ubuntu-latest
       steps:
         - uses: ryanduguid/release-policy/.github/actions/secrets-scan@<commit> # vX.Y.Z
   ```

   The action takes no inputs and needs a Linux x64 runner. It supports the 4 events
   above and `pull_request_target`, and fails on any other.

2. `.github/secrets-rules.toml`, a byte-for-byte copy of
   [`rules.toml`](../.github/actions/secrets-scan/rules.toml) at the pinned commit.

3. `.gitleaks.toml`, which extends that copy and holds only the repository's own
   allowlists:

   ```toml
   title = "my-repository"

   [extend]
   path = ".github/secrets-rules.toml"
   ```

   An allowlist may name the shared rules in `targetRules`. `.gitleaksignore` is
   optional.

The copy exists for local use: a pre-commit hook or a scan of a checkout runs
`betterleaks git . --config .gitleaks.toml` from the repository root, and betterleaks
reads `extend.path` relative to the working directory. The action never reads the copy.
It scans with its own `rules.toml` and fails when the copy in the scanned commit differs,
so local scans and the gate cannot drift apart.

## What it scans

| Event | Commits | Configuration read from |
| --- | --- | --- |
| `push` | `before..after` | the pushed commit |
| `pull_request` | `base..head`, twice | the merge commit, then the base commit |
| `pull_request_target` | `base..head`, twice | the head commit, then the base commit |
| `schedule`, `workflow_dispatch` | the full history | the scanned commit |

Every scan reads `.gitleaks.toml` and `.gitleaksignore` from the named commit's git
objects and runs in an empty folder, so the work tree, `.gitattributes`,
`.betterleaks.toml` and `.betterleaksignore` are never read.

On a pull request the second scan is the gate. It uses the base commit's `.gitleaks.toml`
and `.gitleaksignore` and ignores `gitleaks:allow` and `betterleaks:allow` comments, so a
pull request cannot allowlist what it adds: a new allowlist entry or fingerprint must
reach the base first, in its own pull request. The first scan uses the merge commit's
configuration, which proves a changed `.gitleaks.toml` still loads before it becomes the
base that every later gate reads.

Every scan passes `--text` to Git, so a file Git would take for binary, such as text with
a NUL byte in it, is still read line by line. Text in another encoding, such as UTF-16, and
compressed files are not.

A range that holds a merge of 3 or more parents fails, because Git shows nothing for what
such a merge adds. So does a pull request whose range holds no commit, and one whose base
has no `.gitleaks.toml`: add the configuration in a first pull request and the workflow in
a second.

Before each scan the action pipes a fabricated ABN, `98 765 432 100`, through the composed
configuration and fails unless it is reported. A configuration that stops extending the
shared rules, or allowlists that value, therefore fails instead of passing quietly.

## Limits

- On `pull_request` a pull request runs the workflow file it carries, so one that removes
  the step removes the gate. On `pull_request_target` the workflow file and the action pin
  come from the base branch: the action then fetches the pull request's commits as git
  objects, checks out none of its files and runs none of its code. That fetch is
  unauthenticated, so it needs a public repository.
- betterleaks skips its default paths (lockfiles, images, PDFs and the like) for every
  rule, the client-identifier rules included.
- The scan covers what Git shows as added lines. It does not scan release assets, issue
  text or pull request descriptions.

## Updating

The betterleaks version and the SHA-256 of its Linux x64 archive are the first 2
assignments in [`install.sh`](../.github/actions/secrets-scan/install.sh). The
`Betterleaks release` workflow compares that version with the latest release each week and
files an issue when they differ. A bump is one pull request here that changes both values
and passes `tests/test_secrets_scan.sh`.

A change reaches a caller when its pin moves. Callers pin the commit of a release tag, with
the version in a trailing comment, so Dependabot proposes the next release's commit and
rewrites the comment, at its first weekly run after the caller's cooldown for that release
has passed. When that release changed `rules.toml`, the proposed pull request
fails until the new file is copied to `.github/secrets-rules.toml` in the same pull request.
