# Attribution checks

The composite action at `.github/actions/no-ai-attribution/action.yml` checks
pull request titles and bodies, commit messages, and raw author and committer
identities. It retains the existing matcher and its executable title regression
cases. Technical product references and fenced examples keep their current
treatment.

## Caller contract

Call the action from a step using
`ryanduguid/release-policy/.github/actions/no-ai-attribution@<full-commit-sha>`.
The commit must be available on GitHub before a consumer uses it. Review the
policy revision and migrate each consumer separately. Do not use a branch, tag
or checkout of pull request code to select the action implementation.

Keep the consumer's `No AI attribution` workflow and its two jobs:

- `legacy`, named `check`, runs on `pull_request` with read permissions.
- `policy`, named `Attribution policy runner`, runs on `pull_request_target`,
  pushes to `main` and manual dispatch. Only this job has `statuses: write`.

Both jobs use the shared action. The action checks out the caller repository at
`github.event.pull_request.base.sha || github.sha` with full history. It fetches
missing commit objects as data, reads raw commit headers and messages, and never
executes files from the pull request. It accepts no configurable command or ref.

Keep status reporting in the caller. The trusted job first posts a pending
`Attribution policy` status on a pull request head, or `Attribution audit` on a
push or manual dispatch. After the action, it publishes the job's success or
failure, including when checkout or scanning fails. Cancellation retains the
existing behaviour. The read-only pull request job never posts a commit status.
Preserve the event types and per-event concurrency group too.

The composite action keeps the existing `check` job name. A reusable workflow
would add a caller job to the displayed check name. No branch-protection or
account-permission change is needed for this migration.

## Verification and rollout

The standard policy unittest command runs the action's embedded scanner against
fabricated Git repositories, including rejected metadata, raw identities,
initial pushes and unreadable commit ranges. The repository baseline pins the
action and this guide. Action steps require Bash, Python 3 and Git; the caller's
status steps also require the GitHub CLI. Current callers use `ubuntu-latest`.

Publish the shared action first, then prepare callers pinned to that exact
commit. Keep pending and final status steps unchanged when replacing the
checkout and scanner steps with the action call. Verify clean and rejected pull
requests, including a fork pull request, on GitHub before broad rollout.

Local checks do not prove GitHub's action resolution, token permissions or
required-check reporting. Those need hosted validation after publication is
authorised. Existing private-repository protection limits remain unchanged.
