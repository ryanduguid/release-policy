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
   IDs, model IDs, provider names, billed usage and coverage in `review.json`.
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

GitHub does not inherit Actions workflows from an account's `.github`
repository. Install a pinned caller in every repository you own. For PRs in
other people's public repositories, run local capture and review; their
maintainers control their checks and merge rules. Capture is read-only unless
`--publish-pending` is supplied. The local `review` command writes its report
to disk. Posting a status is a separate explicit `publish` command.

```bash
python scripts/pr_review.py capture --repo OWNER/REPO --pr PR_NUMBER \
  --policy .github/pr-review-policy.json --policy-sha "$PUBLISHED_POLICY_SHA" \
  --snapshot "$REVIEW_DIRECTORY/snapshot.json" --gitleaks "$GITLEAKS_BINARY"
# Use the pinned engine's Python for the model step, once the key is provisioned.
"$PR_AGENT_PYTHON" scripts/pr_review.py review \
  --policy .github/pr-review-policy.json --policy-sha "$PUBLISHED_POLICY_SHA" \
  --snapshot "$REVIEW_DIRECTORY/snapshot.json" --report "$REVIEW_DIRECTORY/review.json"
```

## Models, routes and spending

The only permitted models are `z-ai/glm-5.3` and `xiaomi/mimo-v2.6-pro`.
PR-Agent's review prompt, token budget and output type are reused from commit
`1d01f24f455bb879c1d9c557ad7de3d72dcc7975` of
[PR-Agent](https://github.com/The-PR-Agent/pr-agent), tagged v0.46.0.
The dependency lock is installed with uv 0.12.10. PR-Agent's package metadata
at that tag says 0.45.0; the commit is the version boundary.
The adapter preserves the native review rules and type definitions, and
replaces its YAML example and user response prefix with JSON instructions
before tokenisation. A
changed native prompt layout fails before inference.
Both prompts include the same strict JSON schema. GLM requests schema mode;
MiMo requests JSON object mode after schema requests repeated complete results
until truncation during qualification. Local and native
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
12,288 output tokens per chunk for reasoning and the final answer. A review
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

The Dependabot bridge takes the PR number from the triggering run's REST
`pull_requests` record. It requires exactly one association, the current head
and the actual Dependabot author. The trigger path must match exactly, with
an optional nonempty GitHub `@ref` suffix. A shared commit cannot select
another PR. Missing or ambiguous run associations stop the review.

The model step receives frozen data and the model key. It cannot write a
GitHub status. It runs no PR code, loads no PR settings, follows no PR links
and has no reviewer, helper or model fallback. The two models receive
identical prompts without PR author rationale or the other model's verdict.
The native engine must match the pin and have no changed tracked engine files
or secret configuration files.

Every completion must end normally, identify the expected model and provider,
report billed usage and parse as strict JSON against PR-Agent's review type.
Duplicate keys, missing fields, invalid finding locations, partial chunks,
empty responses and truncated output fail. Reported costs cannot exceed the
reserved ceiling. No automatic paid retries run.

The publisher has no model key. It checks the context/policy/engine hashes,
both distinct models, every chunk and the current head/base before success.
Any finding, security concern or cautious verdict produces a failing advisory
status. Failed, skipped and cancelled jobs also fail. Forced cancellation or
a failed GitHub API write can leave the status pending; pending is never a
passing review. After resolving the failure, start a fresh manual dispatch.

Reruns of the trigger and paid workflows are rejected before model calls.
The shared workflow guards every job with `run_attempt == 1`; the command
also rejects reruns before source reads or status mutation. Rerunning a job
therefore cannot spend the key again or overwrite its earlier status. Local
commands remain available outside Actions.

Evidence artefacts expire after one day. Model output is escaped in the job
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

Keep reference labels out of prompts. Have a person compare each finding
with source and a reproduction or existing confirmed human evidence. Record
precision, must-block defect recall, noise on clean controls, location accuracy,
cost and latency for each model. Repeat serious defects and controls. Do not
use Claude, Codex or an LLM judge. GLM is the provisional lead; the benchmark
must determine whether either model earns that role. No model accuracy result
has been established. Complete live route and event checks before activation.
