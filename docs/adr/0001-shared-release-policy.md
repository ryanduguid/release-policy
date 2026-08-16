# ADR-0001: Shared release-policy module as a dedicated repository of reusable workflows

- Status: Accepted
- Date: 2026-08-16
- Deciders: Ryan Duguid
- Source: account architecture survey, 16 August 2026, candidate 1

## Context

Ten repositories implement the same release policy independently: tag gates, checksums, SBOM generation, provenance attestations and a draft-then-publish lifecycle. The duplication is measurable at the current heads:

- Three packaged-Python workflows (`au-tax-change-impact-monitor`, `monthly-close-control-plane`, `xero-ai-review-gateway`) are 100 lines each and differ only in the wheel filename stem and the version-extraction command.
- An 88-line release-archive builder is byte-identical in four repositories; its test is byte-identical in three; a draft-release lookup is duplicated in two.
- The two skill-pack repositories carry a 309-line hardened variant of the same policy (semver tag regex, API-based main check, dirty-tree gate, paginated no-overwrite check). The policy already exists at three maturity levels, and fixes land family by family instead of once.

All ten repositories received release-hardening work on 15 August 2026, and roughly ten per-repo hardening draft PRs are still open against the same files.

## Decision

Create a dedicated repository, `ryanduguid/release-policy`, that owns release policy as reusable GitHub Actions workflows (`workflow_call`) plus testable gate scripts. Consumer repositories keep a thin caller workflow pinned to a full 40-character commit SHA of the module.

Key properties:

1. **Ownership.** The module repository owns the policy. Consumers never reference a branch or tag of the module, only an immutable commit SHA, bumped by reviewed PR.
2. **Permission isolation.** Read-only conformance jobs run with `contents: read`. The publish job requires `contents: write`, `attestations: write` and `id-token: write`, granted only by the calling repository's workflow. The module cannot acquire permissions a caller does not grant.
3. **Failure policy.** Fail closed. A failed gate stops the release; the module never overwrites an existing release, never retries into a publish, and never creates tags. Creating the annotated tag remains the human approval act.
4. **Blast radius.** A defective module version can block a consumer's next release run. It cannot alter consumer code, existing releases or published assets. Recovery is repointing one SHA.
5. **Canonical gate set.** The hardened skill-pack variant becomes the single policy core; the weaker Python-family gates are retired.
6. **Supersession.** The open per-repo release-hardening draft PRs that this module obsoletes are closed as each consumer migrates, with a comment linking this ADR. The exact PR list is compiled at migration time.
7. **Pilot.** The three packaged-Python repositories migrate first. The source-archive family and skill packs follow in later phases.

## Alternatives considered

- **Composite actions in `.github`.** Rejected. Composite actions cannot own job-level `permissions` or `concurrency`, so the most security-relevant part of the policy would remain duplicated in every caller, and the `.github` repository's documented community-health role would broaden.
- **Shared Python release tool.** Rejected. OIDC attestation and permissions must stay in workflow YAML regardless, splitting the policy across two layers, and a pip-installable tool adds its own supply-chain surface.

## Consequences

- One review updates release policy for every consumer; drift between families ends at the module boundary.
- Consumers accept a cross-repository dependency on a pinned, reviewed commit of the module.
- The module repository must hold itself to the same standard: it releases with its own policy, and its CI must exercise the gates without publishing.
- Repositories with no release workflow (`.github`, `DrDebits`, profile) are out of scope. DrDebits is a possible later adapter but a different artifact type.
