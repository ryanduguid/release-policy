# Independent PR reviews with your own API key

This workflow runs GLM 5.3 and MiMo V2.6 Pro independently against the same
frozen PR evidence. It starts advisory. It does not approve or merge PRs.
Tests, linters, security checks and human review remain necessary.

## Provision and install

1. Publish the reviewed policy change and record its full commit SHA. The
   commit must contain `.github/workflows/pr-review.yml`; a SHA from before
   this change cannot run it. Consumer repositories migrate separately.
2. The supplied policy uses `spending_mode: payg`, as authorised for the
   existing OpenRouter key. Put the key in
   an Actions secret named `OPENROUTER_API_KEY` in each participating repository,
   or use an organisation secret with access restricted to those repositories.
   The workflow passes this one secret explicitly. It does not use
   `secrets: inherit`. Never put a key in source, a PR, an issue or a Hub note.
3. Copy `examples/pr-review-consumer.yml` into the consumer as
   `.github/workflows/pr-review-consumer.yml`. This filename also works in
   release-policy, whose `pr-review.yml` defines the reusable workflow.
   Replace the all-zero SHA with the
   reviewed full SHA. Copy `examples/pr-review-trigger.yml` as
   `.github/workflows/pr-review-trigger.yml`. Inspect and commit these files
   through the consumer's normal review process.
4. Check the consumer's Actions policy permits the pinned actions and reusable
   workflow, and that its public `pull_request_target` policy meets
   [GitHub's requirements](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
   GitHub's announced enforcement date is 2 November 2026.
5. Run a manual review of an open, non-draft public PR. Confirm both generation
   ID digests, model IDs, provider names, billed usage and coverage in `review.json`.
   Inspect the publisher's summary and the status on that exact head commit.
   Test key limit/reset metadata, exhaustion and concurrent capped requests,
   missing keys, stale revisions, provider failures, forks, Dependabot,
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
  --snapshot "$REVIEW_DIRECTORY/snapshot.json" --gitleaks "$GITLEAKS_BINARY"
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

## Models, routes and spending

The only permitted models are `z-ai/glm-5.3` and `xiaomi/mimo-v2.6-pro`.
PR-Agent's review prompt, token budget and output type are reused from commit
`1d01f24f455bb879c1d9c557ad7de3d72dcc7975` of
[PR-Agent](https://github.com/The-PR-Agent/pr-agent), tagged v0.46.0.
The dependency lock is installed with uv 0.12.10, without cache reads. The
workflow omits project installation and imports PR-Agent from the trusted pinned
checkout through `PYTHONPATH`. Its lock has no other local or editable package.
The runtime sync rejects source builds. The two source-only dependencies,
`giteapy==1.0.8` and `html2text==2024.2.26`, use separate hash-verified archives
from that lock, fixed backend wheels and an offline install with no dependency
resolution or build isolation. The backend packages are then removed and the
complete runtime lock is checked again before the model key is injected.
Another missing wheel stops installation. These pinned dependencies remain
executable third-party code in the later reviewer process. PR-Agent's package metadata
at that tag says 0.45.0; the commit is the version boundary.
The adapter preserves the native review rules and type definitions, and
replaces its YAML example and user response prefix with JSON instructions
before tokenisation. A
changed native prompt layout fails before inference.
Both requests include the same strict JSON schema in schema mode;
an explicit nesting example keeps all four required fields inside `review`.
The fixed Xiaomi route must pass exact-schema qualification before a caller
migrates to this source. The previous JSON object request could produce valid
JSON that failed native validation. There is no schema downgrade or repair.
Local and native
validators check every response, including fields, priority tags and source
locations.

| Model | Pinned OpenRouter route | Maximum input / output per million tokens |
| --- | --- | --- |
| GLM 5.3 | `parasail/fp8` | US$1.40 / US$4.40 |
| MiMo V2.6 Pro | `xiaomi/fp8` | US$0.435 / US$0.87 |

These route ceilings were checked on 2 October 2026 against OpenRouter's
[GLM endpoint metadata](https://openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints)
and [MiMo endpoint metadata](https://openrouter.ai/api/v1/models/xiaomi/mimo-v2.6-pro/endpoints).
Every request explicitly selects FP8 serving, uses PR-Agent's configured
temperature of zero, disables routing fallbacks, requires parameter support,
denies provider data collection and caps prices. An unexpected returned model or
provider fails the review. GLM requests high reasoning effort; MiMo requests
reasoning enabled, because its advertised controls differ.

The trusted policy explicitly selects PAYG spending. The runner authenticates
the key before inference and accepts an uncapped key only when both `limit`
and `limit_remaining` are present and null. A key with a limit must report a
positive finite limit and sufficient finite remaining budget. PAYG does not
require a particular reset period or BYOK setting, and does not claim that
those settings bound spending.

For a provider-enforced key limit, a reviewed policy can select
`spending_mode: capped`. That mode requires BYOK charges to count towards the
limit and permits only a nonrenewing limit up to US$100 or a monthly limit up
to US$350. Its reset and BYOK guards follow
[OpenRouter's key contract](https://openrouter.ai/docs/api/api-reference/api-keys/create-keys).

The runner uses a conservative local reservation estimate from the complete
request payload, including its response schema and a fixed safety margin, plus up to
32,768 output tokens per chunk for reasoning and the final answer. This
allowance is a qualification candidate until immutable live canaries complete.
A review
may use at most 12 chunks and reserve at most US$3. Source that exceeds this
limit needs human review. Actual billed usage is recorded after each call;
an unexpected bill stops another call when its remaining reservation no
longer fits. Requests have no automatic paid retries.

The US$3 software reservation limit applies to one review. Bills are checked
after responses; a provider that exceeds its advertised price or token limit
can charge more before the review fails. In PAYG mode, concurrent runs and
other key activity accumulate charges without an account-wide or monthly
cap. Provider price and token limits constrain each request; the reservation
is not a shared account ledger. Funding fees and charges on other provider
accounts are outside the recorded OpenRouter inference bill.

The provisional US$350 monthly forecast is a planning estimate while PAYG is
enabled. The earlier forecast of about US$0.105 per pair assumed
40,000 input and 6,000 billed output tokens for each model. Actual cost depends
on source size, reasoning, revisions and provider billing. Do not replace a
failed request with another model.

Nous is not enabled for unattended reviews. Its
[routing contract](https://hermes-agent.nousresearch.com/docs/integrations/nous-portal)
allows backend changes and says OpenRouter-specific provider preferences may
be ignored. It needs verified price, parameter and model identity controls
before activation. Private repositories are blocked before file
contents are fetched. Private support needs verified zero data retention
routes for both models and explicit source-sharing authorisation. The current
policy cannot be changed to enable private reviews by flipping a flag.

## Evidence and failure behaviour

Capture reads GitHub REST at the captured head and merge base. It includes
complete before/after source for every changed text file, including removals
and renames. It checks pagination, changed-line counts and the revision again
before writing the snapshot. Excluded credential paths, binary files,
unavailable blobs, truncated patches and excessive source stop the run.
Gitleaks 8.30.1 scans raw source before it can enter a model request.

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
transmits every admitted record to each authorised route. Response identity and
coverage checks establish structural completion, without provider-internal
attestation or model accuracy. Universal PR coverage and understanding across
separate chunks remain unqualified. Fragmentation is not enabled; related
regions of each accepted file remain together in one request to each route.

The Dependabot bridge takes the PR number from the triggering run's REST
`pull_requests` record. It requires exactly one association, the current head
and the actual Dependabot author. The trigger path must match exactly, with
an optional nonempty GitHub `@ref` suffix. A shared commit cannot select
another PR. Missing or ambiguous run associations stop the review.

The model step receives frozen data and the model key. It cannot write a
GitHub status. It runs no PR code, loads no PR settings, follows no PR links
and has no reviewer, helper or model fallback. Requests to both routes contain
identical prompts without PR author rationale or the other model's verdict.
The native engine must match the pin and have no changed tracked engine files
or secret configuration files.

Every completion must end normally, identify the expected model and provider,
report billed usage and parse as strict JSON against PR-Agent's review type.
Duplicate keys, missing fields, invalid finding locations, partial chunks,
empty responses and truncated output fail. An unexpected bill invalidates the
review and stops later calls; its recorded value is preserved. No automatic
paid retries run.

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
response with unknown ID and bill. The upload step runs even after ordinary validation failures.
A malformed response therefore retains earlier and current known generation
ID digests, token counts and bills. A transport failure can leave an intended request
with an unknown ID and bill; a missing bill is never recorded as zero.

Receipt journal version 2 adds `diagnostic_category` to every call. It is null
before validation and for accepted output. Rejected output records exactly one
fixed category for the first failed stage:

- `completion_contract` covers model/provider, finish, usage and generation checks.
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
paths, arbitrary keys, values and excerpts are never included. Old version-one
receipts remain unclassified; their original failures cannot be reconstructed.
There is no output repair, coercion, fence removal or paid retry.

Receipts contain the planned model/provider and chunk hash, transport and
validation states, the fixed diagnostic category, a generation ID digest, identity-match booleans, an allowed
finish reason and finite usage numbers. They exclude source, prompts, response
content, unexpected identity strings, headers and raw errors. Receipt write
failures stop subsequent calls. The journal cannot authorise a verdict, release
a reservation or replay an inference. Forced cancellation or runner loss can
prevent upload; receipt preservation in those cases is best effort.
The final journal write must succeed before a complete report is written.
Both publishers require the model job to succeed, including receipt upload,
before downloading or assessing a complete report. Receipt failures can veto
completeness; receipts cannot establish it.

Receipt journal version 3 adds `usage.reasoning_tokens`. The only permitted
source is `usage.completion_tokens_details.reasoning_tokens`. An exact
non-negative integer must fit both the valid completion count and the requested
output allowance. Missing, invalid or excessive values become null. No reasoning
text or other nested usage fields are retained. The count is diagnostic: it
cannot change billing, release a reservation, authorise another call, accept
output or select a status. Old journals do not supply this measurement; an
absent count is never interpreted as zero. Public report schema 2 is unchanged.

Production commands write receipt journal version 4. It preserves the version
3 fields and adds `generation_metadata`. After the original response receipt
is durable, the runner may make one GET to OpenRouter's
[generation metadata endpoint](https://openrouter.ai/docs/api/api-reference/generations/get-generation)
when a valid ID and matching original identity exist. A fixed trusted subprocess
has a 15-second execution
deadline, covering connection and complete body consumption, plus a 10-second
socket timeout and a 65,536-byte response limit. Credentials and the raw ID pass
only through its private stdin; output contains the sanitised projection.
The process uses no shell and is killed and waited for on timeout. Process
creation and operating-system cleanup add scheduling overhead to the deadline.
There is no redirect or retry. Immediate record availability is
unqualified; a failed attempt stays unknown because the raw ID is discarded.

The lookup must match the exact generation ID and planned model/provider, plus
any identity supplied in the original envelope. It retains only its fixed state,
a decimal USD cost string and provider-reported latency/generation time in
milliseconds. Both timing keys must be present and may be null under the
[published API schema](https://openrouter.ai/openapi.json); valid values must be
finite, non-negative and no greater than 86,400,000 milliseconds. The namespace
excludes raw IDs, unexpected identity strings, content, headers and error bodies.
Lookup states distinguish awaiting response, ineligible ID, lookup intent,
unavailable, rejected and verified. Lookup intent is saved before dispatch;
an interrupted attempt can retain that state without proving whether transport
began. Terminal lookup states describe helper results, not proof that a GET
was sent. Receipt replacement failures stop execution; existing atomic receipts
remain available.

Lookup success is optional; durable receipts are mandatory for completion.
A metadata receipt replacement failure vetoes the report and stops later
inference, even when the original response is valid. The recorded values are
prospective diagnostics. They cannot repair a completion,
release a reservation, authorise another paid call, select a route or publish
a verdict. Standalone adapters without a lookup retain version 3. Existing
unknown bills cannot be recovered from generation ID hashes.

Public report schema 2 and journals publish `generation_id_sha256`, the full
lower-case SHA-256 digest of the exact validated ASCII provider ID. Raw IDs
remain transient for response validation and distinct-call checks. Legacy raw
ID fields are refused. The digest permits equality comparison and owner-side
matching to a privately known provider record; it cannot be used directly for
provider lookup. Cross-account lookup scope has not been verified, so raw IDs
are withheld from new reports and receipts.

Anyone who knows or can guess an ID can reproduce its digest and correlate
records. A digest does not prove receipt, account ownership, independent
execution, billing accuracy or content provenance. Earlier schema 1 reports
already exposed raw IDs for one day; hashing does not revoke that disclosure.

Model output is escaped in the job
summary. The workflow publishes statuses and a summary; it does not post PR
comments. Provider prompts, source and error bodies are absent from logs.

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
  --output "$BENCHMARK_DIRECTORY" --limit 10 --gitleaks "$GITLEAKS_BINARY"
```

Public GitHub API access needs enough read quota. Once the pinned PR-Agent
environment and authorised OpenRouter key are provisioned, use its Python and add
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

After changing a prompt, schema, policy or route candidate, run the local
contract regressions and prepare the fixed set using a fresh output directory:

```bash
python -m unittest discover -s tests -p "test_pr_review*.py" -v
python scripts/pr_review_benchmark.py \
  --cases benchmarks/pr-review-controls.json \
  --policy .github/pr-review-policy.json --policy-sha "$POLICY_SHA" \
  --output control-snapshots --limit 6
```

Set `POLICY_SHA` to the reviewed full source commit and make the pinned
`gitleaks` executable available as described above. Preparation makes no model
calls; synthetic snapshots have no live publication capability. Unit tests use
fabricated outputs. Passing them cannot qualify provider behaviour, deletion
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
bills even when generation digests repeat. The summary flags those repeated
digests. It accepts sanitised journal versions 2, 3 and 4 and sums their recorded
decimal bills. Those numbers can already contain upstream rounding. Missing
bills on intended calls remain unknown; `accounting_complete` covers only the
selected recorded attempts. Funding fees, other account activity and lost
journals are outside that result. The output cannot overwrite an input journal.
Version 4 resolves a missing response bill from verified lookup evidence for
measurement only. Equal decimal bills count once; unequal bills stay visible
in their original receipt fields and count as an unresolved billing conflict.
The summary reports response-only, lookup-only, corroborated, unknown and
conflicting observations. Conflicts are excluded from the known subtotal and
keep accounting incomplete. This completeness field has no spending authority.

Per-model counts separate planned, unstarted, received, accepted, rejected and
indeterminate calls. Rates include their integer numerator and denominator;
an empty denominator has no rate. Completion of both models is reported
separately. Early failure makes later model observations a selected subset,
so these counts cannot establish which model reviews code better. The
`strict_schema` category still combines native, local and finding-location
checks. Version 4 summaries provide provider-reported timing medians with sample
counts and attempted-generation denominators. These exclude missing timing
values and do not measure complete review time or human accuracy.

Keep reference labels out of prompts. Have a person compare each finding
with source and a reproduction or existing confirmed human evidence. Record
precision, must-block defect recall, noise on clean controls, location accuracy,
cost and latency for each model. Repeat serious defects and controls. Do not
use Claude, Codex or an LLM judge. GLM is the provisional lead; the benchmark
must determine whether either model earns that role. No model accuracy result
has been established. Complete live route and event checks before activation.
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
known inference subtotal, unknown bill count and assessment minutes. Count
shared time for reading source and reconciling findings once. Cost per confirmed defect
is unavailable when bills are unknown or the defect count is zero. Assessed
precision is unavailable when its denominator is zero. These measurements
support a later human comparison; they do not establish a lead model by themselves.
