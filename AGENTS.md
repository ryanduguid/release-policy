# Release policy repository instructions

Before changing a workflow, gate or policy document, read
[CONTRIBUTING.md](CONTRIBUTING.md) and the relevant consumer guide under `docs/`.
The digest contract in `tests/test_repository_baseline.sh` identifies protected
policy documents. A reviewed change to one must update its digest and explain
the policy change; preserve all unrelated digests.

Consumers pin full commit SHAs and migrate separately. Preserve the distinction
between source review, recorded canary evidence and a verified release. Read
`canaries.json` when making a claim about a completed consumer run.

Before handoff, run the applicable checks from CONTRIBUTING.md and
[ci.yml](.github/workflows/ci.yml). Use fabricated evidence in tests. A passing
policy check does not authorise a consumer migration, tag, upload or publication.
