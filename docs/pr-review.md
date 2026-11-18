# Claude PR reviews with your own API key

This workflow runs Claude Opus 5.5 and Claude Sonnet 5.5 separately, through
Anthropic's API, against the same frozen PR evidence. Both reviewers are
Claude models from one vendor. They are not independent of each other, and
neither is independent of a PR that Claude wrote. The status keeps its
existing name, **Independent PR review (advisory)**, for existing callers.
It starts advisory. It does not approve or merge PRs.
Tests, linters, security checks and human review remain necessary.

## Provision and install

1. Publish the reviewed policy change and record its full commit SHA. The
   commit must contain `.github/workflows/pr-review.yml`; a SHA from before
   this change cannot run it. Consumer repositories migrate separately.
2. Put an Anthropic API key in
   an Actions secret named `ANTHROPIC_API_KEY` in each participating repository,
   or use an organisation secret with access restricted to those repositories.
   The workflow passes this one secret explicitly. It does not use
   `secrets: inherit`. Never put a key in source, a PR, an issue or a Hub note.
3. Copy `examples/pr-review-consumer.yml` into the consumer as
   `.github/workflows/pr-review-consumer.yml`. This filename also works in
   release-policy, whose `pr-review.yml` defines the reusable workflow.
   Replace the all-zero SHA with the
   reviewed full SHA. This prints the commit of this repository's latest
   release:

       gh api repos/ryanduguid/release-policy/commits/$(gh api repos/ryanduguid/release-policy/releases/latest --jq .tag_name) --jq .sha

   Copy `examples/pr-review-trigger.yml` as
   `.github/workflows/pr-review-trigger.yml`. Inspect and commit these files
   through the consumer's normal review process.
4. Check the consumer's Actions policy permits the pinned actions and reusable
   workflow, and that its public `pull_request_target` policy meets
   [GitHub's requirements](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
   GitHub's announced enforcement date is 2 November 2026.
5. Run a manual review of an open, non-draft public PR. Confirm both generation
   ID digests, model IDs, token usage, computed costs and coverage in `review.json`.
   Inspect the publisher's summary and the status on that exact head commit.
   Test missing keys, a refused token count, an over-budget PR,
   stale revisions, API failures, forks, Dependabot,
   PRs that share a commit, rejected reruns and cancellation before rolling out to
   other repositories.

Keep **Independent PR review (advisory)** out of required checks during the
pilot. The workflow deliberately refuses an enforcement mode. Making it a
merge gate needs a separate reviewed change, a live event matrix, acceptable
benchmark results, a status source restriction and strict requirements to
update the PR when its base branch advances. A GitHub commit status cannot
atomically attest that the base branch stayed unchanged after publication.

Automatic paid reviews cover PRs authored by the account that owns the
personal repository and verified Dependabot PRs. The author login comes from
GitHub's API and must match the repository owner. Other contributors and PRs
in organisation repositories need someone with repository Write access or
greater to start a fresh manual workflow. This explicitly authorises a paid
review. GitHub's Maintain/Admin roles are not required.

The caller skips ordinary owner PRs in the Dependabot bridge and other authors
in the automatic owner path. Its event prefilters do not grant authority: the
core verifies actual authors and run associations through REST before source
capture or spending. The bridge automatically accepts runs started by
Dependabot. A person reopening a Dependabot PR must use a fresh manual dispatch.

The trigger job runs only for first attempts whose original actor has
GitHub.com account ID `49699333`. It skips owner-started PR events, including
events on Dependabot-authored PRs, and all reruns. The direct owner route above
remains available. Skipped jobs can report success, so the caller's upstream-actor
filter remains necessary alongside the REST admission checks. This schedules
the runner; it does not establish a paid review or measured savings.

The caller's prefilter compares the PR author's numeric ID with the current
repository owner's ID and the trigger actor's ID with GitHub.com's Dependabot
account `49699333`, verified through REST on 5 October 2026. Missing IDs reject
the automatic path. This fixed bot identity requires a reviewed update if GitHub
changes accounts. The bridge is disabled outside GitHub.com; GitHub Enterprise
Server remains unqualified. Verify both
event paths without inference or status publication before activating a changed
caller; the downstream REST checks still decide admission.

GitHub does not inherit Actions workflows from an account's `.github`
repository. Install a pinned caller in every repository you own. For PRs in
other people's public repositories, use the central report workflow below or
run local capture and review. Their maintainers control their checks and merge
rules. Capture is read-only unless
`--publish-pending` is supplied. The local `review` command writes its report
to disk. Posting a status is a separate explicit `publish` command.

```bash
python scripts/pr_review.py capture --repo OWNER/REPO --pr PR_NUMBER \
  --policy .github/pr-review-policy.json --policy-sha "$PUBLISHED_POLICY_SHA" \
  --snapshot "$REVIEW_DIRECTORY/snapshot.json" --betterleaks "$BETTERLEAKS_BINARY" \
  --gitleaks-rules "$GITLEAKS_RULES"
# Use the locked environment and pinned engine source for the model step.
PYTHONPATH="$PR_AGENT_SOURCE" "$PR_AGENT_PYTHON" scripts/pr_review.py review \
  --policy .github/pr-review-policy.json --policy-sha "$PUBLISHED_POLICY_SHA" \
  --snapshot "$REVIEW_DIRECTORY/snapshot.json" --report "$REVIEW_DIRECTORY/review.json"
```

## Central reports for public upstream PRs

Install `examples/pr-review-central.yml` as
`.github/workflows/pr-review-central.yml` in `ryanduguid/release-policy`, replacing
the zero SHA with the full reviewed source commit. This personal pilot accepts
one public GitHub PR URL per fresh manual dispatch on `main`. The numeric actor
and repository IDs are fixed to Ryan and this policy repository. The target
must be a different public repository, with an open, non-draft PR authored by
Ryan's numeric user ID. There is no scheduled or bulk dispatch.

The central capture and summary jobs have read permissions. Every target REST
request uses a fixed GitHub API origin, no authentication and no redirects.
Source is frozen and secret-scanned through the existing collector. The
target repository ID, author, head/base repository IDs and commits are bound
to the snapshot, then checked again before inference and rendering. Repository renames,
deletion of a head repository, author or revision changes and private targets
stop the run. A review remains a report in the policy repository's Actions
summary; central snapshots cannot enter the status publisher or failure path.
No upstream comments, statuses or approvals are posted.

GitHub validates the reusable workflow's skipped installed jobs against the
caller's permission maximum. The caller therefore declares `pull-requests:
read` and `statuses: write` for the policy repository. Each executed central
capture, model and summary job explicitly reduces that maximum to its required
read scopes. The token is scoped to the policy repository; the different
target repository receives no authenticated requests or writes. A read-only
caller was rejected before jobs started in the permission qualification,
run 37073390988.

The same installer and two-model review job serve installed and central runs.
Unknown workflow modes fail before source access or inference. A current head
and base check is not an atomic attestation of a branch that may advance later.
If a model job fails, the summary states that the review is incomplete and
provides no verdict. A complete review containing findings is shown as such.

## Models and spending

The only permitted models are `claude-opus-5-5` and `claude-sonnet-5-5`,
called in that order through Anthropic's
[Messages API](https://platform.claude.com/docs/en/api/messages/create) with
API version `2026-10-21`.
PR-Agent's review prompt, token budget and output type are reused from commit
`06a2991c0ce6b055c77fbcbd7f09faf4c5acfcaf` of
[PR-Agent](https://github.com/The-PR-Agent/pr-agent), the first commit whose
uv requirement accepts uv 0.13.4 or later, which GHSA-hq85-5cpj-mr62 requires.
Later releases rename the merge recommendation values this report uses.
The dependency lock is installed with uv 0.13.9, without cache reads. The
workflow omits project installation and imports PR-Agent from the trusted pinned
checkout through `PYTHONPATH`. Its lock has no other local or editable package.
Optional provider integrations, including the source-only `giteapy`, are not
installed. The runtime sync rejects source builds. The source-only runtime
dependency, `html2text==2024.2.26`, uses a separate hash-verified archive
from that lock, fixed backend wheels and an offline install with no dependency
resolution or build isolation. The backend packages are then removed and the
complete runtime lock is checked again before the model key is injected.
Another missing wheel stops installation. These pinned dependencies remain
executable third-party code in the later reviewer process. The runner removes
the key from its environment before it imports PR-Agent. PR-Agent's package metadata
at that commit says 0.47.0; the commit is the version boundary.
The adapter preserves the native review rules and type definitions, and
replaces its YAML example and user response prefix with JSON instructions
before tokenisation. A
changed native prompt layout fails before inference.
Both requests include the same strict JSON schema as a structured output;
an explicit nesting example keeps all four required fields inside `review`.
Anthropic refuses length, numeric and array-limit keywords in that schema, so
the request omits `minLength`, `maxLength`, `maxItems` and `minimum`.
There is no schema downgrade or repair. Local and native
validators check every response, including those limits, fields, priority
tags and source locations.

Each request uses adaptive thinking at high effort and caps thinking and
answer together at 16,000 output tokens. It requests global inference, which
is billed at standard prices and overrides a workspace default of US-only
inference. API version `2026-10-21` removes temperature, so none is sent.
Requests use no fallbacks, tools, prompt caching, fast mode or service tier.
An unexpected returned model fails the review.

| Model | Input / output per million tokens |
| --- | --- |
| Claude Opus 5.5 | US$4 / US$20 |
| Claude Sonnet 5.5 | US$2 / US$10 |

These are the list prices on Anthropic's
[pricing page](https://platform.claude.com/docs/en/about-claude/pricing),
checked on 18 November 2026. A price change needs a reviewed policy change.

An ordinary API key cannot read its balance or spending limit, so the runner
does not check them. It reserves, for every planned call of both models, the
complete request size in bytes plus 1,000 input tokens and the 16,000-token
output cap, at the policy's prices. A review may use at most 12 chunks and
reserve at most US$3; otherwise it stops before any call. One full
180,000-byte chunk fits. Two chunks fit only when their requests total about
340,000 bytes or less. Source that exceeds this limit needs human review.

Before the first paid call, the runner sends each planned request to
Anthropic's free
[token counting endpoint](https://platform.claude.com/docs/en/build-with-claude/token-counting).
A failed count, or a count above the request's size in bytes, stops the review
before any paid call. Anthropic caps output at the requested 16,000 tokens,
but describes the count as an estimate that may differ from billed input by a
small amount, without a stated maximum. The allowance is the request's size
in bytes plus 1,000 tokens; Anthropic's models page puts 1 million tokens at
about 2.5 million characters, so it is usually more than twice the count. No
Anthropic document guarantees a token ceiling for a byte count, so the input
side of the US$3 bound rests on that counter and is not a guarantee.

After each response, the runner requires input tokens within the reservation,
output tokens within the cap, no cache tokens, and standard speed and service
tier. It computes the call's cost from those counts at the list prices and
rounds it up. Any breach fails the review and stops later calls; its recorded
value is preserved. A call over its allowance is billed before the runner
rejects it. The computed cost is not Anthropic's bill, which the
Anthropic Console shows. Requests have no automatic paid retries.

The US$3 reservation applies to one review. It is not an account-wide cap:
concurrent runs and other use of the same key accumulate charges outside it,
as would Anthropic billing that differs from its list prices. Do not replace
a failed request with another model.

Anthropic's
[API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)
page, read on 18 November 2026, says retained data is never used for model
training without express permission, and that conversation content (prompts
and Claude's outputs) is not retained by default, except for Covered Models
(Claude Fable and Claude Mythos), which this workflow does not use. Content flagged by Anthropic's automated
trust and safety systems may be retained for up to 2 years. Structured output
schemas are cached for up to 24 hours since last use.

Private repositories are blocked before file contents are fetched. Private
support needs a verified data retention arrangement and explicit
source-sharing authorisation. The current policy cannot be changed to enable
private reviews by flipping a flag.

## Evidence and failure behaviour

Capture reads GitHub REST at the captured head and merge base. It includes
complete before/after source for every changed text file, including removals
and renames. It checks pagination, changed-line counts and the revision again
before writing the snapshot. Excluded credential paths, binary files,
unavailable blobs, truncated patches and excessive source stop the run.
Betterleaks 1.10.1 scans raw source twice before it can enter a model request:
once with the rules of Gitleaks 8.30.1 and once with its own default rules,
extended by `.github/pr-review-betterleaks.toml`. A finding in either scan
stops the run. A `gitleaks:allow` or `betterleaks:allow` comment in the source
exempts nothing. `--gitleaks-rules` names the Gitleaks rules, which are
`config/gitleaks.toml` at its `v8.30.1` tag; without the option, capture reads
`gitleaks-8.30.1.toml` in the working directory. It refuses a file with any
other SHA-256. Two things Gitleaks stopped need more than its rules: it decoded
a run of hex digits that other base64 characters follow, and it ended its
chunks at other offsets. So each scan also reads 2 copies of the source, with
a line break after such digits. A copy's only blank lines are those added
after each 16,000 bytes, 8,000 bytes apart in the 2 copies, so lines that
together take under 8,000 bytes fall within one chunk of one copy.

Each chunk contains unsplit complete file records and is limited to 180,000
canonical UTF-8 bytes, including source, diff, paths and JSON escaping. The
planner includes every changed file before inference. The publisher recomputes
the same ordered chunk hashes from the frozen snapshot and requires an accepted
result for every chunk from both models. The snapshot's policy hash binds the
size limit; changing the limit requires a fresh snapshot. Missing, reordered or substituted
chunks fail coverage validation. Source exceeding the file, chunk, complete
request or spending ceiling stops the entire review before inference.
Findings use absolute after-source lines, or before-source lines for removed
files. They must refer to a file supplied in that same call.

The larger ceiling expands whole-file eligibility. The client constructs and
transmits every admitted record to each model. Response identity and
coverage checks establish structural completion, without provider-internal
attestation or model accuracy. Universal PR coverage and understanding across
separate chunks remain unqualified. Fragmentation is not enabled; related
regions of each accepted file remain together in one request to each model.

The Dependabot bridge takes the PR number from the triggering run's REST
`pull_requests` record. It requires exactly one association, the current head
and the actual Dependabot author. The trigger path must match exactly, with
an optional nonempty GitHub `@ref` suffix. A shared commit cannot select
another PR. Missing or ambiguous run associations stop the review.

The model step receives frozen data and the model key. It cannot write a
GitHub status. It runs no PR code, loads no PR settings, follows no PR links
and has no reviewer, helper or model fallback. Requests to both models contain
identical prompts without PR author rationale or the other model's verdict.
The native engine must match the pin and have no changed tracked engine files
or secret configuration files.

Every completion must end normally (`end_turn`), identify the expected model,
report token usage at standard rates, contain one text answer and parse as
strict JSON against PR-Agent's review type. Refusals, output cut at the token
cap, tool calls, duplicate keys, missing fields, invalid finding locations,
partial chunks and empty responses fail. Usage beyond the reservation
invalidates the review and stops later calls; its recorded value is preserved.
No automatic paid retries run.

The publisher has no model key. It checks the context/policy/engine hashes,
both distinct models, every chunk and the current head/base before success.
Any finding, security concern or cautious verdict produces a failing advisory
status. The publisher exits zero after a validated report, summary and status
write complete, including a failing advisory verdict. Its exit code reports
publication success; the commit status and report carry the advisory verdict.
Consumer gates must read that verdict, not infer it from the publisher job's
success. Summary rendering and local writes precede the status API call.
Missing or invalid reports, stale revisions, rendering errors and failed API
writes exit nonzero and invoke the generic execution-failure status. Completed
findings retain their specific description. The fallback checks the publisher
step's operational outcome. Failed, skipped and cancelled review jobs also fail. Forced cancellation or
a failed GitHub API write can leave the status pending; pending is never a
passing review. After resolving the failure, start a fresh manual dispatch.

An installed target that is already closed before capture can end as
`closed_before_admission` after repository, public-access, commit and trigger
checks pass. Drafts, including closed drafts, remain ineligible. This trusted
same-run disposition skips source retention, both models and the publisher;
the summary says that no review occurred. It writes no pending or clean status.
Only the exact `captured` disposition permits installed inference. A missing
or invalid capture output fails the publisher's guard. Closure after pending
was written remains an execution failure. Workflow success describes operational
handling and cannot substitute for the advisory commit status or a validated
report. Central public capture retains its existing eligibility checks.

Reruns of the trigger and paid workflows are rejected before model calls.
The shared workflow guards every job with `run_attempt == 1`; the command
also rejects reruns before source reads or status mutation. Rerunning a job
therefore cannot spend the key again or overwrite its earlier status. Local
commands remain available outside Actions.

Source and complete review artefacts expire after one day. Sanitised receipt
artefacts expire after 30 days. The runner atomically saves a bounded journal
before each intended paid request and saves response metadata before parsing
model content. A content-free `response_observed` transition is saved before
reading or decoding the response envelope; malformed JSON leaves an observed
response with unknown ID and cost. The upload step runs even after ordinary validation failures.
A malformed response therefore retains earlier and current known message
ID digests, token counts and computed costs. A transport failure can leave an intended request
with an unknown ID and cost; a missing cost is never recorded as zero.

Every receipt call records a `diagnostic_category`. It is null
before validation and for accepted output. Rejected output records exactly one
fixed category for the first failed stage:

- `completion_contract` covers model, stop reason, content, usage and message ID checks.
- `json_syntax` covers JSON decoding failures.
- `duplicate_keys` covers repeated decoded object members, including nested objects.
- `root_or_nesting` covers a non-object root or a present `review` container that is not an object.
- `strict_schema` covers native and local schema, type, required-field and finding-location checks.

An unexpected exception inside output acceptance records `unexpected_runtime`
with `output_state: indeterminate`. It does not prove invalid model output and
must not count as a model format failure. Transport, reservation and billed-cost
ceilings, receipt writes, capture identity, rendering and status operations
retain their separate failure paths.
The categories come from trusted validation stages. Exception messages, field
paths, arbitrary keys, values and excerpts are never included.
There is no output repair, coercion, fence removal or paid retry.

Receipts contain the planned model and chunk hash, transport and
validation states, the fixed diagnostic category, a message ID digest, a model-match boolean, an allowed
stop reason, token counts and the computed cost. They exclude source, prompts, response
content, unexpected identity strings, headers and raw errors. Receipt write
failures stop subsequent calls. The journal cannot authorise a verdict, release
a reservation or replay an inference. Forced cancellation or runner loss can
prevent upload; receipt preservation in those cases is best effort.
The final journal write must succeed before a complete report is written.
Both publishers require the model job to succeed, including receipt upload,
before downloading or assessing a complete report. Receipt failures can veto
completeness; receipts cannot establish it.

Production commands write receipt journal version 5. Its `usage` holds
`input_tokens`, `output_tokens`, `thinking_tokens` and `cost`. The only
permitted source for `thinking_tokens` is
`usage.output_tokens_details.thinking_tokens`; an exact non-negative integer
must fit both the output count and the output cap, or it becomes null. The
count is diagnostic: it cannot change the cost, release a reservation,
authorise another call, accept output or select a status. `cost` is computed
from the 2 counts at the policy's list prices and rounded up. It is null when
either count is missing or the response reports cache tokens, fast speed or
another service tier; a missing cost is never zero. No billing lookup is made:
Anthropic's usage and cost reports need an Admin key, which the workflow does
not hold.

Public report schema 3 and journals publish `generation_id_sha256`, the full
lower-case SHA-256 digest of the exact validated ASCII message ID. Raw IDs
remain transient for response validation and distinct-call checks. Legacy raw
ID fields are refused. The digest permits equality comparison and owner-side
matching to a privately known message record; it cannot be used directly for
lookup. Raw IDs are withheld from new reports and receipts.

Anyone who knows or can guess an ID can reproduce its digest and correlate
records. A digest does not prove receipt, account ownership, independent
execution, billing accuracy or content provenance. Earlier schema 1 reports
already exposed raw IDs for one day; hashing does not revoke that disclosure.

Model output is escaped in the job
summary. The workflow publishes statuses and a summary; it does not post PR
comments. Prompts, source, error bodies and the key are absent from logs.

## Benchmark before choosing a lead model

`benchmarks/pr-review-cases.json` contains 196 pinned public PR cases from
[AACR-Bench](https://github.com/alibaba/aacr-bench/tree/68a569759289a83654a59d06db2a72910edf0a4a)
and 4 synthetic clean controls. The original positive dataset's Git blob was
verified before deriving the manifest. Its 705 reference defect labels include
human and model-origin comments retained by the dataset. They are reference
evidence requiring human adjudication, not proof that every label is correct.
The dataset's Apache 2.0 licence is preserved in `benchmarks/LICENSE-AACR`.

Prepare a small batch without paid calls:

```bash
python scripts/pr_review_benchmark.py \
  --cases benchmarks/pr-review-cases.json \
  --policy .github/pr-review-policy.json --policy-sha "$PUBLISHED_POLICY_SHA" \
  --output "$BENCHMARK_DIRECTORY" --limit 10 --betterleaks "$BETTERLEAKS_BINARY" \
  --gitleaks-rules "$GITLEAKS_RULES"
```

Public GitHub API access needs enough read quota. Once the pinned PR-Agent
environment and authorised Anthropic key are provisioned, use its Python and add
`--run` to make paid calls. Use `--offset` and `--limit` to run measured batches;
use a fresh output directory for repetitions. Preparation refuses unsupported
comparisons and oversized cases. Record those as coverage failures, never
as clean reviews. The four synthetic controls occupy offsets 196 to 199.

`benchmarks/pr-review-controls.json` is the fixed six-case regression set. It
reuses those four clean controls and adds a deleted guard in a modified file
and a removed file. Both deletion cases still need human assessment. They
exercise source capture and location representation, not measured model recall.
The native format supplies a file and line range without a side field. For a
modified file, validation uses the current file's bounds; a deleted old line
outside those bounds is rejected. For a removed file, it uses the original
file's bounds. The adapter does not silently move a finding to another line.
An accepted location establishes only valid bounds, not semantic correctness.

After changing a prompt, schema, policy or model candidate, run the local
contract regressions and prepare the fixed set using a fresh output directory:

```bash
python -m unittest discover -s tests -p "test_pr_review*.py" -v
python scripts/pr_review_benchmark.py \
  --cases benchmarks/pr-review-controls.json \
  --policy .github/pr-review-policy.json --policy-sha "$POLICY_SHA" \
  --output control-snapshots --limit 6
```

Set `POLICY_SHA` to the reviewed full source commit and make the pinned
`betterleaks` executable and the Gitleaks rules available as described above.
Preparation makes no model
calls; synthetic snapshots have no live publication capability. Unit tests use
fabricated outputs. Passing them cannot qualify model behaviour, deletion
recall or human accuracy. Paid reruns require the existing spending authority
and resolved billing holds. Keep the same six case identities when comparing
candidate runs, alongside their candidate, context, report and receipt hashes.

Each batch saves `benchmark_batch_v2` before preparing its first case. Its
records include every selected immutable case, initially `unstarted`, and the
policy, engine, schema and adapter identities. Atomic checkpoints distinguish
preparation, model and report-storage failures. The first failure stops the
batch; later cases remain unstarted. A saved `preparing` or `reviewing` state
after interruption is incomplete evidence. A failed or completed batch cannot
resume paid execution. A prepared batch can reuse checked snapshots only with
the same configuration and selected cases. Repetitions need a fresh directory.

Summarise explicitly selected sanitised journals without source or model calls:

```bash
python scripts/pr_review_measurements.py \
  --journal attempt1=first.review.receipts.json \
  --journal attempt2=second.review.receipts.json \
  --output operational-summary.json
```

Use a stable, unique attempt identity, such as a workflow run ID and its attempt
number. Supply one final journal per attempt. Identical copies with the same
identity count once; conflicting copies fail. Separate attempts keep their
costs even when message digests repeat. The summary flags those repeated
digests. It accepts only sanitised journal version 5 and sums its computed
costs, which are not Anthropic's bill. Missing
costs on intended calls remain unknown; `accounting_complete` covers only the
selected recorded attempts. Other account activity and lost
journals are outside that result. The output cannot overwrite an input journal.
This completeness field has no spending authority.

Per-model counts separate planned, unstarted, received, accepted, rejected and
indeterminate calls. Rates include their integer numerator and denominator;
an empty denominator has no rate. Completion of both models is reported
separately. Early failure makes later model observations a selected subset,
so these counts cannot establish which model reviews code better. The
`strict_schema` category still combines native, local and finding-location
checks. The summary does not measure review time or human accuracy.

Keep reference labels out of prompts. Have a person compare each finding
with source and a reproduction or existing confirmed human evidence. Record
precision, must-block defect recall, noise on clean controls, location accuracy,
cost and latency for each model. Repeat serious defects and controls. Do not
use Claude, Codex or an LLM judge. The benchmark must determine whether either
model earns a lead role. No model accuracy result
has been established. Complete live model and event checks before activation.
Record confirmed true, confirmed false, indeterminate and unassessed findings
separately, with evidence and immutable case/report identities. Known misses
are not recall unless a human has established the full relevant defect set.

To measure each reviewer's contribution, have a person assign stable defect
identities within each case and link confirmed true findings to those identities.
Repeated findings about one defect count once in defect coverage; record their
duplicate and assessment workload separately. Compare shared and model-only
defect sets only when both models completed the same case and all their findings
were adjudicated. An unstarted, failed or unassessed second review cannot prove
that the first model found a unique defect. Keep partial coverage visible.

Record each model's confirmed false, indeterminate and unassessed findings,
known computed subtotal, unknown cost count and assessment minutes. Count
shared time for reading source and reconciling findings once. Cost per confirmed defect
is unavailable when costs are unknown or the defect count is zero. Assessed
precision is unavailable when its denominator is zero. These measurements
support a later human comparison; they do not establish a lead model by themselves.
