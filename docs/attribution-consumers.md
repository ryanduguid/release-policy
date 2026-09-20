# Attribution checks

The composite action at `.github/actions/no-ai-attribution/action.yml` checks
pull request titles and bodies, commit messages, and raw author and committer
identities. It retains the existing matcher and its executable title regression
cases. Technical product references and fenced examples keep their current
treatment. Commit messages and pull request text are read as written and again
with Git comment markers peeled and whitespace-indented trailer continuations
joined onto their trailer, so a credit kept behind a `#` line by
`--cleanup=whitespace` or split across a folded trailer is caught by the action
as well as by the local `.githooks/commit-msg` guard.

The reusable workflow at `.github/workflows/attribution-policy.yml` wraps the
action with the status reporting every consumer needs: a pending `Attribution
policy` status on a pull request head, or `Attribution audit` on a push or
manual dispatch, then the job's success or failure, including when checkout or
scanning fails. It is the one place that wrapper lives.

## Caller contract

Call the reusable workflow from a single job using
`ryanduguid/release-policy/.github/workflows/attribution-policy.yml@<full-commit-sha>`.
The commit must be available on GitHub before a consumer uses it. Review the
policy revision and migrate each consumer separately. Do not use a branch or
tag to select the workflow. This repository's own caller uses the local path
`./.github/workflows/attribution-policy.yml`, so it always runs the revision it
ships.

Keep the consumer's `No AI attribution` workflow to one job:

- `policy`, named `Attribution policy`, runs on `pull_request_target`, pushes
  to every branch and manual dispatch, and grants the called workflow
  `contents: read`, `pull-requests: read` and `statuses: write`.

The push trigger covers every branch, not only `main`. A commit pushed straight
to a side branch would otherwise go unaudited until it reached `main`, and a
branch that never opens a pull request would never be audited at all. On a new
branch the push event carries no base, so the action scans the branch's whole
history; that is the intended behaviour and is why the trigger suits a
repository whose history is already clean.

A consumer that is a contribution fork omits the push trigger and runs on
`pull_request_target` and manual dispatch only. A fork's history carries
upstream authorship that the fork neither owns nor rewrites, so scanning an
upstream sync would fail on commits that are not the fork's to fix. Pull
requests opened in the fork are still checked, because the action reads only
the pull request commit range.

Keep the event types and the per-event concurrency group in the caller. A
called workflow cannot set workflow-level concurrency, but concurrency on a job
inside it does apply, and a group that matches the caller's can cancel the
caller. The caller carries no steps of
its own and no `pull_request` trigger: the status posted from the trusted base
branch is the gate, and branch protection requires the `Attribution policy`
commit status, not a job name.

The action checks out the caller repository at
`github.event.pull_request.base.sha || github.sha` with full history. It fetches
missing commit objects as data, reads raw commit headers and messages, and never
executes files from the pull request. It accepts no configurable command or ref.

Until 13 September 2026 each caller also ran a `legacy` job named `check` on
`pull_request`, which ran the same action a second time from the pull request
head and was a required context of its own. That job and its required context
were removed; the trusted-branch status remains required everywhere.

## Verification and rollout

The standard policy unittest command runs the action's embedded scanner against
fabricated Git repositories, including rejected metadata, raw identities,
initial pushes and unreadable commit ranges. The repository baseline pins the
action, the reusable workflow and this guide. Action steps require Bash,
Python 3 and Git; the status steps also require the GitHub CLI. Current callers
use `ubuntu-latest`.

The workflow pins the composite action to a commit on `main`, and
`tests/test_attribution_pin.py` checks on every push that the pin is reachable
and carries the action on disk. A change to the action therefore lands in two
steps: merge the action change, then repin the workflow to that merge commit.

Coordinate publication with portfolio-governance: first add the shared
workflow's merge commit to its `release_policy_pins` attribution-policy
allowlist while retaining the old approved pin. Then publish the shared
workflow and pin callers to that exact commit. After all consumers migrate,
remove the old allowlisted pin. Verify clean and rejected pull requests,
including a fork pull request, on GitHub before broad rollout.

Local checks do not prove GitHub's workflow resolution, token permissions or
required-check reporting. Those need hosted validation after publication is
authorised. Existing private-repository protection limits remain unchanged.
