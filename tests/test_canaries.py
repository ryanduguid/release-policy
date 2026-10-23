from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import check_canaries

SHA = "1" * 40
OLDER_SHA = "2" * 40
HEAD = "3" * 40


def manifest(
    *,
    current_sha: str = SHA,
    evidence_sha: str = SHA,
    family: str = "python",
    ref: str = "v1.2.3",
    **entry_fields: object,
) -> dict[str, object]:
    return {
        "schema": 1,
        "families": [
            {
                "family": family,
                "repository": "ryanduguid/example",
                "workflow_path": ".github/workflows/release.yml",
                "policy_workflow": (
                    ".github/workflows/verify-skills.yml"
                    if family == "verify"
                    else f".github/workflows/release-{family}.yml"
                ),
                "current_policy_sha": current_sha,
                "evidence": {
                    "run_id": 123,
                    "head_sha": HEAD,
                    "ref": ref,
                    "policy_sha": evidence_sha,
                    "started_at": "2026-08-27T00:00:00Z",
                },
                **entry_fields,
            }
        ],
    }


def run_payload(
    *,
    policy_sha: str = SHA,
    run_id: int = 123,
    family: str = "python",
    ref: object = "v1.2.3",
) -> dict[str, object]:
    policy_workflow = (
        "verify-skills.yml" if family == "verify" else f"release-{family}.yml"
    )
    return {
        "id": run_id,
        "conclusion": "success",
        "event": "push",
        "head_branch": ref,
        "head_sha": HEAD,
        "path": ".github/workflows/release.yml",
        "run_started_at": "2026-08-27T00:00:00Z",
        "referenced_workflows": [
            {
                "path": (
                    "ryanduguid/release-policy/.github/workflows/"
                    f"{policy_workflow}@{policy_sha}"
                ),
                "sha": policy_sha,
            }
        ],
    }


class CanaryManifestTests(unittest.TestCase):
    def test_root_tags_remain_valid_with_an_omitted_or_empty_prefix(self) -> None:
        for family in ("archive", "python", "skills", "verify"):
            ref = "main" if family == "verify" else "v1.2.3"
            for fields in ({}, {"tag_prefix": ""}):
                with self.subTest(family=family, fields=fields):
                    parsed = check_canaries.parse_manifest(
                        manifest(family=family, ref=ref, **fields)
                    )
                    self.assertEqual(parsed[0].tag_prefix, "")
                    self.assertEqual(parsed[0].evidence.ref, ref)

    def test_accepts_explicit_namespaces_for_archive_and_python(self) -> None:
        for family in ("archive", "python"):
            for ref in ("example-tool/v0.0.0", "example-tool/v1.2.3"):
                with self.subTest(family=family, ref=ref):
                    parsed = check_canaries.parse_manifest(
                        manifest(family=family, ref=ref, tag_prefix="example-tool")
                    )
                    self.assertEqual(parsed[0].tag_prefix, "example-tool")
                    self.assertEqual(parsed[0].evidence.ref, ref)

    def test_rejects_malformed_prefixes_and_unsupported_families(self) -> None:
        invalid_prefixes = (
            None, False, 42, [], {}, " ", "../tool", "./tool", "foo/bar",
            "foo\\bar", "/foo", "foo/", "Foo", "foo_bar", "foo.bar",
            "-foo", "foo-", "foo--bar", "foo\n",
        )
        for prefix in invalid_prefixes:
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(ValueError, "tag_prefix"):
                    check_canaries.parse_manifest(
                        manifest(ref="example-tool/v1.2.3", tag_prefix=prefix)
                    )
        for family in ("skills", "verify"):
            with self.subTest(family=family):
                with self.assertRaisesRegex(ValueError, "tag_prefix"):
                    check_canaries.parse_manifest(
                        manifest(family=family, ref="main", tag_prefix="example-tool")
                    )

    def test_rejects_tags_outside_the_opted_in_namespace(self) -> None:
        for fields in ({}, {"tag_prefix": ""}):
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, "canonical version tag"):
                    check_canaries.parse_manifest(
                        manifest(ref="example-tool/v1.2.3", **fields)
                    )
        for ref in (
            "v1.2.3", "other-tool/v1.2.3", "example-tool-extra/v1.2.3",
            "example-tool//v1.2.3", "example-tool/../v1.2.3",
            "example-tool/nested/v1.2.3", "Example-tool/v1.2.3",
            "example-tool/1.2.3", "example-tool/v01.2.3",
            "example-tool/v1.02.3", "example-tool/v1.2.03",
            "example-tool/v1.2", "example-tool/v1.2.3-rc1",
            "example-tool/v1.2.3+build", "example-tool/v1.2.3\n",
            "refs/tags/example-tool/v1.2.3",
        ):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "canonical version tag"):
                    check_canaries.parse_manifest(
                        manifest(ref=ref, tag_prefix="example-tool")
                    )

    def test_optional_prefix_does_not_allow_unknown_or_missing_keys(self) -> None:
        for document in (
            None,
            {"schema": 1},
            manifest(tag_prefix="example-tool", extra=True),
            manifest(tag_prefixes="example-tool"),
            {"schema": 1, "families": [{"tag_prefix": "example-tool"}]},
        ):
            with self.subTest(document=document):
                with self.assertRaisesRegex(ValueError, "must contain"):
                    check_canaries.parse_manifest(document)

    def test_live_namespaced_audit_ignores_unrelated_or_invalid_successes(self) -> None:
        for family in ("archive", "python"):
            with self.subTest(family=family):
                recorded = run_payload(family=family, ref="example-tool/v1.2.3")
                failed = run_payload(family=family, ref="example-tool/v1.2.4")
                failed["conclusion"] = "failure"
                irrelevant = [
                    None, failed,
                    *[
                        run_payload(family=family, run_id=456, ref=ref)
                        for ref in (
                            None, 123, "main", "v9.9.9", "other-tool/v9.9.9",
                            "example-tool/v01.2.3", "example-tool/v1.2.3-rc1",
                        )
                    ],
                ]

                def fetch_json(endpoint: str) -> object:
                    if "/compare/" in endpoint:
                        return {"status": "behind"}
                    if endpoint.endswith("/actions/runs/123"):
                        return recorded
                    return {"workflow_runs": [*irrelevant, recorded]}

                result = check_canaries.check_live(
                    check_canaries.parse_manifest(
                        manifest(family=family, ref="example-tool/v1.2.3",
                                 tag_prefix="example-tool")
                    ),
                    fetch_json=fetch_json,
                    fetch_text=lambda _: (
                        "uses: ryanduguid/release-policy/.github/workflows/"
                        f"release-{family}.yml@{SHA}\n"
                    ),
                )
                self.assertEqual(result.errors, ())
                self.assertEqual(result.warnings, ())

    def test_live_namespaced_audit_rejects_stale_missing_or_wrong_evidence(self) -> None:
        recorded = run_payload(ref="example-tool/v1.2.3")
        sibling = run_payload(ref="other-tool/v1.2.3", run_id=456)
        newer = run_payload(ref="example-tool/v1.2.4", run_id=789)
        for evidence, runs, message in (
            (recorded, [newer, recorded], "latest successful run"),
            (recorded, [sibling], "no relevant successful workflow run"),
            (sibling, [recorded], "evidence run head_branch"),
        ):
            with self.subTest(message=message):
                def fetch_json(endpoint: str) -> object:
                    if "/compare/" in endpoint:
                        return {"status": "behind"}
                    if endpoint.endswith("/actions/runs/123"):
                        return evidence
                    return {"workflow_runs": runs}

                result = check_canaries.check_live(
                    check_canaries.parse_manifest(
                        manifest(ref="example-tool/v1.2.3", tag_prefix="example-tool")
                    ),
                    fetch_json=fetch_json,
                    fetch_text=lambda _: (
                        "uses: ryanduguid/release-policy/.github/workflows/"
                        f"release-python.yml@{SHA}\n"
                    ),
                )
                self.assertTrue(any(message in error for error in result.errors))
                self.assertEqual(result.warnings, ())

    def test_live_audit_orders_successes_by_start_time(self) -> None:
        # GitHub has listed an older success ahead of newer ones; the audit must
        # still compare the newest relevant success with the recorded run.
        recorded = run_payload()
        older = {
            **run_payload(run_id=100, ref="v1.2.2"),
            "run_started_at": "2026-08-01T00:00:00Z",
        }

        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return recorded
            return {"workflow_runs": [older, recorded]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(manifest()),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"release-python.yml@{SHA}\n"
            ),
        )
        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())

    def test_live_legacy_release_and_verify_select_only_their_expected_refs(self) -> None:
        for family in ("archive", "python", "skills", "verify"):
            with self.subTest(family=family):
                ref = "main" if family == "verify" else "v1.2.3"
                recorded = run_payload(family=family, ref=ref)
                policy_workflow = (
                    "verify-skills.yml" if family == "verify" else f"release-{family}.yml"
                )

                def fetch_json(endpoint: str) -> object:
                    if "/compare/" in endpoint:
                        return {"status": "identical"}
                    if endpoint.endswith("/actions/runs/123"):
                        return recorded
                    return {"workflow_runs": [
                        run_payload(family=family, ref="other/v9.9.9", run_id=456),
                        recorded,
                    ]}

                result = check_canaries.check_live(
                    check_canaries.parse_manifest(manifest(family=family, ref=ref)),
                    fetch_json=fetch_json,
                    fetch_text=lambda _: (
                        "uses: ryanduguid/release-policy/.github/workflows/"
                        f"{policy_workflow}@{SHA}\n"
                    ),
                )
                self.assertEqual(result.errors, ())
                self.assertEqual(result.warnings, ())

    def test_validates_shape_and_rejects_duplicates_or_bad_sha(self) -> None:
        parsed = check_canaries.parse_manifest(manifest())
        self.assertEqual([entry.family for entry in parsed], ["python"])

        duplicate = manifest()
        duplicate["families"] = duplicate["families"] * 2  # type: ignore[operator]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            check_canaries.parse_manifest(duplicate)

        invalid = manifest(current_sha="main")
        with self.assertRaisesRegex(ValueError, "full lower-case SHA"):
            check_canaries.parse_manifest(invalid)

    def test_live_check_accepts_current_pin_and_latest_exact_success(self) -> None:
        requests: list[str] = []

        def fetch_json(endpoint: str) -> object:
            requests.append(endpoint)
            if endpoint == f"repos/ryanduguid/release-policy/compare/main...{SHA}":
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return run_payload()
            if "/actions/workflows/" in endpoint:
                return {"workflow_runs": [run_payload()]}
            raise AssertionError(endpoint)

        def fetch_text(endpoint: str) -> str:
            requests.append(endpoint)
            return (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"release-python.yml@{SHA}\n"
            )

        result = check_canaries.check_live(
            check_canaries.parse_manifest(manifest()),
            fetch_json=fetch_json,
            fetch_text=fetch_text,
        )

        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())
        self.assertEqual(len(requests), 4)

    def test_detects_stale_current_pin_and_newer_success(self) -> None:
        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return run_payload()
            return {"workflow_runs": [run_payload(run_id=456), run_payload()]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(manifest()),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"release-python.yml@{OLDER_SHA}\n"
            ),
        )

        self.assertTrue(any("current pin" in error for error in result.errors))
        self.assertTrue(any("latest successful" in error for error in result.errors))

    def test_records_prior_pin_release_evidence_as_an_explicit_warning(self) -> None:
        document = manifest(current_sha=SHA, evidence_sha=OLDER_SHA)

        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return run_payload(policy_sha=OLDER_SHA)
            return {"workflow_runs": [run_payload(policy_sha=OLDER_SHA)]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(document),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"release-python.yml@{SHA}\n"
            ),
        )

        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("current pin lacks release evidence", result.warnings[0])

    def test_verify_accepts_a_newer_success_running_the_current_pin(self) -> None:
        """The verification consumer runs on every push to its default branch,
        so a success newer than the recorded evidence is the ordinary state
        rather than drift. What has to hold is that the newest success is
        running the pin the consumer declares today."""
        recorded = run_payload(family="verify", ref="main")
        newer = run_payload(family="verify", ref="main", run_id=456)

        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return recorded
            return {"workflow_runs": [newer, recorded]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(manifest(family="verify", ref="main")),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"verify-skills.yml@{SHA}\n"
            ),
        )

        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())

    def test_verify_rejects_a_latest_success_on_a_superseded_pin(self) -> None:
        """A consumer that declares a pin its own workflow has never run
        successfully is the state this family exists to catch."""
        recorded = run_payload(family="verify", ref="main")
        superseded = run_payload(
            family="verify", ref="main", run_id=456, policy_sha=OLDER_SHA
        )

        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return recorded
            return {"workflow_runs": [superseded, recorded]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(manifest(family="verify", ref="main")),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"verify-skills.yml@{SHA}\n"
            ),
        )

        self.assertEqual(len(result.errors), 1)
        self.assertIn("used policy pin", result.errors[0])
        self.assertEqual(result.warnings, ())

    def test_verify_does_not_record_a_release_evidence_warning(self) -> None:
        """`current pin lacks release evidence` names a pin no release has run
        yet, and it clears on that consumer's next authorised release. The
        verification consumer publishes nothing, so the warning would never
        clear on its own, and the current-pin check above already proves the
        thing the warning exists to doubt."""
        recorded = run_payload(family="verify", ref="main", policy_sha=OLDER_SHA)
        newer = run_payload(family="verify", ref="main", run_id=456)

        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return recorded
            return {"workflow_runs": [newer, recorded]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(
                manifest(
                    family="verify",
                    ref="main",
                    current_sha=SHA,
                    evidence_sha=OLDER_SHA,
                )
            ),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"verify-skills.yml@{SHA}\n"
            ),
        )

        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())

    def test_rejects_a_current_pin_unreachable_from_policy_main(self) -> None:
        """A pin can match the consumer's workflow byte for byte and still be
        dead: after a history rewrite of this repository the commit reaches no
        branch or tag, and GitHub refuses the reusable-workflow call before any
        job starts. The audit asks GitHub how the pin sits relative to main and
        accepts only an ancestor."""
        for compare, message in (
            ({"status": "ahead"}, "compare status 'ahead'"),
            ({"status": "diverged"}, "compare status 'diverged'"),
            ({"unexpected": True}, "compare status None"),
            (RuntimeError("HTTP 404: No commit found"), "No commit found"),
        ):
            with self.subTest(message=message):
                def fetch_json(endpoint: str) -> object:
                    if "/compare/" in endpoint:
                        if isinstance(compare, Exception):
                            raise compare
                        return compare
                    if endpoint.endswith("/actions/runs/123"):
                        return run_payload()
                    return {"workflow_runs": [run_payload()]}

                result = check_canaries.check_live(
                    check_canaries.parse_manifest(manifest()),
                    fetch_json=fetch_json,
                    fetch_text=lambda _: (
                        "uses: ryanduguid/release-policy/.github/workflows/"
                        f"release-python.yml@{SHA}\n"
                    ),
                )
                self.assertEqual(len(result.errors), 1)
                self.assertIn("is not reachable from release-policy main", result.errors[0])
                self.assertIn(message, result.errors[0])
                self.assertEqual(result.warnings, ())

    def test_load_rejects_noncanonical_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="canary-manifest-") as directory:
            path = Path(directory) / "canaries.json"
            path.write_text('{"families": [], "schema": 1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "canonical"):
                check_canaries.load_manifest(path)

    def test_ci_validates_manifest_and_runs_live_audit_only_on_schedule(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("python scripts/check_canaries.py", workflow)
        self.assertIn("if: github.event_name == 'schedule'", workflow)
        self.assertIn("python scripts/check_canaries.py --live --require-current-evidence", workflow)
        group = next(
            line.strip().removeprefix("group: ")
            for line in workflow.splitlines()
            if line.strip().startswith("group: ")
        )
        groups = {
            group.replace("${{ github.workflow }}", "CI")
            .replace("${{ github.ref }}", "refs/heads/main")
            .replace("${{ github.event_name }}", event)
            for event in ("schedule", "push", "workflow_dispatch")
        }
        self.assertEqual(len(groups), 3, "live audit must not share a cancellation group")


    def test_live_finds_a_namespaced_run_hidden_behind_a_full_first_page(self) -> None:
        recorded = run_payload(ref="example-tool/v1.2.3")
        irrelevant = [
            run_payload(run_id=900 + index, ref="other-tool/v1.0.0")
            for index in range(100)
        ]

        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return recorded
            if endpoint.endswith("page=1"):
                return {"workflow_runs": irrelevant}
            return {"workflow_runs": [recorded]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(
                manifest(ref="example-tool/v1.2.3", tag_prefix="example-tool")
            ),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"release-python.yml@{SHA}\n"
            ),
        )
        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())

    def test_live_reports_its_own_condition_when_every_page_is_searched(self) -> None:
        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            return {"workflow_runs": [
                run_payload(run_id=900 + index, ref="other-tool/v1.0.0")
                for index in range(100)
            ]}

        result = check_canaries.check_live(
            check_canaries.parse_manifest(
                manifest(ref="example-tool/v1.2.3", tag_prefix="example-tool")
            ),
            fetch_json=fetch_json,
            fetch_text=lambda _: (
                "uses: ryanduguid/release-policy/.github/workflows/"
                f"release-python.yml@{SHA}\n"
            ),
        )
        self.assertTrue(
            any("pages of successful runs" in error for error in result.errors),
            result.errors,
        )
        self.assertFalse(
            any("no relevant successful workflow run" in error for error in result.errors),
            result.errors,
        )


# Retention: GitHub deletes workflow runs after 90 days, so the recorded
# evidence run of every canary eventually returns 404. The clock is injected.
RECORDED = "2026-08-27T00:00:00Z"
BEFORE_EXPIRY = check_canaries.datetime(2026, 11, 25, tzinfo=check_canaries.timezone.utc)
AFTER_EXPIRY = check_canaries.datetime(2026, 11, 27, tzinfo=check_canaries.timezone.utc)
RELEASE_PIN = "uses: ryanduguid/release-policy/.github/workflows/release-python.yml@" + SHA + "\n"
VERIFY_PIN = "uses: ryanduguid/release-policy/.github/workflows/verify-skills.yml@" + SHA + "\n"


def release(tag: str, published_at: str, *, draft: bool = False) -> dict[str, object]:
    return {"tag_name": tag, "published_at": published_at, "draft": draft}


def expired_fetch(
    *,
    status: int = 404,
    releases: object = None,
    listing: object = None,
    details: dict[str, object] | None = None,
):
    def fetch_json(endpoint: str) -> object:
        if "/compare/" in endpoint:
            return {"status": "behind"}
        if endpoint.endswith("/actions/runs/123"):
            raise check_canaries.GitHubError("gone", status=status)
        if "/releases?" in endpoint:
            if isinstance(releases, Exception):
                raise releases
            if releases is None:
                return [release("v1.2.3", "2026-08-27T00:05:00Z")]
            return releases
        if "/actions/workflows/" in endpoint:
            return listing if listing is not None else {"workflow_runs": []}
        for run_id, detail in (details or {}).items():
            if endpoint.endswith(f"/actions/runs/{run_id}"):
                if isinstance(detail, Exception):
                    raise detail
                return detail
        raise AssertionError(endpoint)

    return fetch_json


def audit(document: dict[str, object], fetch_json, *, pin: str = RELEASE_PIN,
          verifier=None, now=AFTER_EXPIRY) -> check_canaries.CheckResult:
    return check_canaries.check_live(
        check_canaries.parse_manifest(document),
        fetch_json=fetch_json,
        fetch_text=lambda _: pin,
        verify_provenance=verifier,
        now=now,
    )


def accept(_: check_canaries.Canary) -> None:
    return None


def verify_run(run_id: int, started: str, *, policy_sha: str = SHA) -> dict[str, object]:
    run = run_payload(run_id=run_id, family="verify", ref="main", policy_sha=policy_sha)
    run["run_started_at"] = started
    run["path"] = ".github/workflows/release.yml"
    return run


class CanaryRetentionTests(unittest.TestCase):
    def test_manifest_requires_a_utc_start_time(self) -> None:
        document = manifest()
        document["families"][0]["evidence"]["started_at"] = "2026-08-27 00:00"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "UTC timestamp"):
            check_canaries.parse_manifest(document)

    def test_a_404_before_retention_ends_is_a_missing_run(self) -> None:
        result = audit(manifest(), expired_fetch(), verifier=accept, now=BEFORE_EXPIRY)
        self.assertTrue(any("missing before its retention" in e for e in result.errors))
        self.assertEqual(result.notices, ())

    def test_other_failures_after_retention_are_not_expiry(self) -> None:
        result = audit(manifest(), expired_fetch(status=500), verifier=accept)
        self.assertTrue(any("could not read evidence run" in e for e in result.errors))
        self.assertEqual(result.notices, ())

    def test_expired_release_with_verified_provenance_passes_with_a_notice(self) -> None:
        checked: list[str] = []
        result = audit(manifest(), expired_fetch(), verifier=lambda c: checked.append(c.family))
        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())
        self.assertEqual(checked, ["python"])
        self.assertIn("signed provenance were verified instead", result.notices[0])

    def test_expired_release_without_a_verifier_fails(self) -> None:
        result = audit(manifest(), expired_fetch())
        self.assertTrue(any("no provenance verifier" in e for e in result.errors))

    def test_failed_provenance_fails_with_or_without_expiry(self) -> None:
        def reject(_: check_canaries.Canary) -> None:
            raise RuntimeError("signature did not verify")

        def retained(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if "/releases?" in endpoint:
                return [release("v1.2.3", "2026-08-27T00:05:00Z")]
            if endpoint.endswith("/actions/runs/123"):
                return run_payload()
            return {"workflow_runs": [run_payload()]}

        for fetch_json in (expired_fetch(), retained):
            result = audit(manifest(), fetch_json, verifier=reject)
            self.assertTrue(
                any("release provenance not verified" in e for e in result.errors), result
            )
            self.assertEqual(result.notices, ())

    def test_live_probe_verifies_provenance_while_the_run_exists(self) -> None:
        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if "/releases?" in endpoint:
                return [release("v1.2.3", "2026-08-27T00:05:00Z")]
            if endpoint.endswith("/actions/runs/123"):
                return run_payload()
            return {"workflow_runs": [run_payload()]}

        checked: list[str] = []
        result = audit(manifest(), fetch_json, verifier=lambda c: checked.append(c.family))
        self.assertEqual((result.errors, result.notices, checked), ((), (), ["python"]))

    def test_a_newer_published_release_needs_review_even_with_a_lower_version(self) -> None:
        releases = [
            release("v1.2.3", "2026-08-27T00:05:00Z"),
            release("v1.0.9", "2026-09-30T00:00:00Z"),
        ]
        result = audit(manifest(), expired_fetch(releases=releases), verifier=accept)
        self.assertTrue(any("NEWER_RELEASE_REVIEW_REQUIRED" in e and "v1.0.9" in e
                            for e in result.errors), result.errors)

    def test_drafts_and_sibling_namespaces_do_not_overtake_the_record(self) -> None:
        releases = [
            release("example-tool/v1.2.3", "2026-08-27T00:05:00Z"),
            release("other-tool/v2.0.0", "2026-10-01T00:00:00Z"),
            release("example-tool/v1.3.0", "2026-10-02T00:00:00Z", draft=True),
        ]
        document = manifest(ref="example-tool/v1.2.3", tag_prefix="example-tool")
        result = audit(document, expired_fetch(releases=releases), verifier=accept)
        self.assertEqual(result.errors, ())

    def test_release_listing_failures_fail_closed(self) -> None:
        full_page = [release(f"other/v{index}.0.0", "2026-09-01T00:00:00Z") for index in range(100)]
        cases = {
            "not a list": {"releases": []},
            "could not read releases": check_canaries.GitHubError("rate limited", 429),
            "pages of releases": full_page,
            "no published release": [release("v1.2.3", "2026-08-27T00:05:00Z", draft=True)],
        }
        for expected, releases in cases.items():
            result = audit(manifest(), expired_fetch(releases=releases), verifier=accept)
            self.assertTrue(any(expected in e for e in result.errors), (expected, result.errors))

    def test_a_retained_newer_release_run_names_the_review_action(self) -> None:
        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return run_payload()
            return {"workflow_runs": [run_payload(run_id=456, ref="v1.2.4"), run_payload()]}

        result = audit(manifest(), fetch_json)
        self.assertTrue(any("NEWER_RELEASE_REVIEW_REQUIRED" in e and "456" in e
                            for e in result.errors), result.errors)

    def test_expired_verify_run_is_replaced_by_a_later_readable_success(self) -> None:
        later = verify_run(789, "2026-11-20T00:00:00Z")
        listing = {"workflow_runs": [later]}
        detail = dict(later, head_branch="main", path=".github/workflows/release.yml")
        result = audit(
            manifest(family="verify", ref="main"),
            expired_fetch(listing=listing, details={789: detail}),
            pin=VERIFY_PIN,
            verifier=accept,
        )
        self.assertEqual(result.errors, ())
        self.assertIn("current success 789", result.notices[0])

    def test_expired_verify_run_without_a_good_later_success_fails(self) -> None:
        later = verify_run(789, "2026-11-20T00:00:00Z")
        good = dict(later, head_branch="main", path=".github/workflows/release.yml")
        cases = [
            ("no later success", verify_run(123, RECORDED), {}),
            ("no later success", verify_run(789, "2026-08-26T00:00:00Z"), {}),
            ("could not read current success", later,
             {789: check_canaries.GitHubError("gone", 404)}),
            ("response is not an object", later, {789: []}),
            ("conclusion", later, {789: dict(good, conclusion="failure")}),
        ]
        for expected, latest, details in cases:
            result = audit(
                manifest(family="verify", ref="main"),
                expired_fetch(listing={"workflow_runs": [latest]}, details=details),
                pin=VERIFY_PIN,
            )
            self.assertTrue(any(expected in e for e in result.errors), (expected, result.errors))
            self.assertEqual(result.notices, ())

    def test_expired_verify_run_on_a_superseded_pin_still_fails(self) -> None:
        later = verify_run(789, "2026-11-20T00:00:00Z", policy_sha=OLDER_SHA)
        result = audit(
            manifest(family="verify", ref="main"),
            expired_fetch(listing={"workflow_runs": [later]}),
            pin=VERIFY_PIN,
        )
        self.assertTrue(any("used policy pin" in e for e in result.errors))

    def test_a_non_object_run_and_an_unreadable_listing_are_reported_once_each(self) -> None:
        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if endpoint.endswith("/actions/runs/123"):
                return []
            raise check_canaries.GitHubError("rate limited", 429)

        result = audit(manifest(), fetch_json)
        self.assertEqual(
            [e for e in result.errors if "not an object" in e or "latest successes" in e],
            [
                "python (ryanduguid/example): evidence run response is not an object",
                "python (ryanduguid/example): could not read latest successes: rate limited",
            ],
        )

    def test_a_null_evidence_run_fails_in_every_mode_and_never_expires(self) -> None:
        # Only a 404 can mark a run expired; a JSON null body is not a run record.
        def null_run_fetch(latest: dict[str, object]):
            def fetch_json(endpoint: str) -> object:
                if "/compare/" in endpoint:
                    return {"status": "identical"}
                if endpoint.endswith("/actions/runs/123"):
                    return None
                if "/releases?" in endpoint:
                    return [release("v1.2.3", "2026-08-27T00:05:00Z")]
                return {"workflow_runs": [latest]}

            return fetch_json

        cases = (
            (manifest(), RELEASE_PIN, null_run_fetch(run_payload()), "python"),
            (
                manifest(family="verify", ref="main"),
                VERIFY_PIN,
                null_run_fetch(verify_run(123, RECORDED)),
                "verify",
            ),
        )
        for document, pin, fetch_json, family in cases:
            for verifier in (None, accept):
                for now in (BEFORE_EXPIRY, AFTER_EXPIRY):
                    with self.subTest(family=family, verifier=verifier, now=now):
                        result = audit(document, fetch_json, pin=pin, verifier=verifier, now=now)
                        self.assertEqual(
                            (result.errors, result.warnings, result.notices),
                            (
                                (f"{family} (ryanduguid/example): evidence run response is not an object",),
                                (),
                                (),
                            ),
                        )

    def test_a_newer_release_without_a_successful_run_fails_while_the_run_exists(self) -> None:
        releases = [
            release("v1.2.3", "2026-08-27T00:05:00Z"),
            release("v1.2.4", "2026-09-30T00:00:00Z"),
            release("other-tool/v9.0.0", "2026-10-01T00:00:00Z"),
        ]

        def fetch_json(endpoint: str) -> object:
            if "/compare/" in endpoint:
                return {"status": "behind"}
            if "/releases?" in endpoint:
                return releases
            if endpoint.endswith("/actions/runs/123"):
                return run_payload()
            return {"workflow_runs": [run_payload()]}

        result = audit(manifest(), fetch_json, verifier=accept, now=BEFORE_EXPIRY)
        self.assertTrue(any("NEWER_RELEASE_REVIEW_REQUIRED" in e and "v1.2.4" in e
                            for e in result.errors), result.errors)
        releases.pop(1)
        result = audit(manifest(), fetch_json, verifier=accept, now=BEFORE_EXPIRY)
        self.assertEqual(result.errors, ())

    def test_expired_verify_run_trusts_only_the_replacement_run_record(self) -> None:
        later = verify_run(789, "2026-11-20T00:00:00Z")
        older_pin = verify_run(789, "2026-11-20T00:00:00Z", policy_sha=OLDER_SHA)
        no_pin = dict(later)
        del no_pin["referenced_workflows"]
        cases = [
            ("id 790 != 789", dict(later, id=790)),
            ("used policy pin", older_pin),
            ("used policy pin", no_pin),
            ("is not later", dict(later, run_started_at=RECORDED)),
            ("is not later", dict(later, run_started_at="soon")),
        ]
        for expected, detail in cases:
            result = audit(
                manifest(family="verify", ref="main"),
                expired_fetch(listing={"workflow_runs": [later]}, details={789: detail}),
                pin=VERIFY_PIN,
            )
            self.assertTrue(any(expected in e for e in result.errors), (expected, result.errors))
            self.assertEqual(result.notices, ())
        result = audit(
            manifest(family="verify", ref="main"),
            expired_fetch(listing={"workflow_runs": [dict(later, id="789")]}),
            pin=VERIFY_PIN,
        )
        self.assertTrue(any("no later success" in e for e in result.errors), result.errors)

    def test_main_prints_notices_without_failing_strict_mode(self) -> None:
        result = check_canaries.CheckResult(errors=(), warnings=(), notices=("expired",))
        with mock.patch.object(check_canaries, "check_live", return_value=result), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(check_canaries.main(["--live", "--require-current-evidence"]), 0)
        printed.assert_any_call("INFO expired")


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


class GitHubReadTests(unittest.TestCase):
    def test_gh_json_returns_the_body_and_reads_the_status_on_failure(self) -> None:
        with mock.patch.object(check_canaries.subprocess, "run") as run:
            run.return_value = completed('HTTP/2.0 200 OK\nX: y\n\n{"a": 1}')
            self.assertEqual(check_canaries._gh_json("repos/x"), {"a": 1})
            run.return_value = completed("HTTP/2.0 404 Not Found\nX: y\n\n{}", 1, "gh: Not Found")
            with self.assertRaises(check_canaries.GitHubError) as caught:
                check_canaries._gh_json("repos/x")
            self.assertEqual((caught.exception.status, str(caught.exception)), (404, "gh: Not Found"))
            run.return_value = completed("", 1, "")
            with self.assertRaises(check_canaries.GitHubError) as caught:
                check_canaries._gh_json("repos/x")
            self.assertIsNone(caught.exception.status)
            self.assertIn("gh api failed", str(caught.exception))
            run.return_value = completed("HTTP/2.0 200 OK\n\nnot json")
            with self.assertRaisesRegex(check_canaries.GitHubError, "invalid JSON"):
                check_canaries._gh_json("repos/x")

    def test_gh_text_returns_raw_content_or_raises(self) -> None:
        with mock.patch.object(check_canaries.subprocess, "run") as run:
            run.return_value = completed("uses: x@y")
            self.assertEqual(check_canaries._gh_text("repos/x/contents/f"), "uses: x@y")
            run.return_value = completed("", 1, "")
            with self.assertRaisesRegex(RuntimeError, "gh api failed"):
                check_canaries._gh_text("repos/x/contents/f")

    def test_run_gh_returns_output_or_raises(self) -> None:
        with mock.patch.object(check_canaries.subprocess, "run") as run:
            run.return_value = completed("ok")
            self.assertEqual(check_canaries._run_gh(["release", "view"]), "ok")
            run.return_value = completed("", 1, "")
            with self.assertRaisesRegex(check_canaries.GitHubError, "gh release failed"):
                check_canaries._run_gh(["release", "view"])
            run.return_value = completed("", 1, "denied")
            with self.assertRaisesRegex(check_canaries.GitHubError, "denied"):
                check_canaries._run_gh(["release", "view"])

    def test_tag_commit_dereferences_annotated_tags_and_refuses_the_rest(self) -> None:
        responses = {
            "repos/r/git/ref/tags/v1": {"object": {"type": "tag", "sha": "t1"}},
            "repos/r/git/tags/t1": {"object": {"type": "commit", "sha": HEAD}},
            "repos/r/git/ref/tags/light": {"object": {"type": "commit", "sha": HEAD}},
            "repos/r/git/ref/tags/blob": {"object": {"type": "blob", "sha": "b"}},
            "repos/r/git/ref/tags/odd": {"object": {"type": "commit", "sha": 7}},
            "repos/r/git/ref/tags/none": {},
            "repos/r/git/ref/tags/loop": {"object": {"type": "tag", "sha": "loop"}},
            "repos/r/git/tags/loop": {"object": {"type": "tag", "sha": "loop"}},
        }
        with mock.patch.object(check_canaries, "_gh_json", side_effect=responses.__getitem__):
            self.assertEqual(check_canaries._tag_commit("r", "v1"), HEAD)
            self.assertEqual(check_canaries._tag_commit("r", "light"), HEAD)
            for tag in ("blob", "odd", "none", "loop"):
                self.assertIsNone(check_canaries._tag_commit("r", tag), tag)

    def test_names_run_matches_the_exact_run_only(self) -> None:
        def result(uri: object) -> dict[str, object]:
            return {"verificationResult": {"signature": {"certificate": {"runInvocationURI": uri}}}}

        base = "https://github.com/o/r/actions/runs/"
        self.assertTrue(check_canaries._names_run([result(base + "123/attempts/2")], "o/r", 123))
        for results in (
            [result(base + "1234/attempts/1")],
            [result(base + "123/attempts/")],
            [result(base + "123")],
            [result(7), {"verificationResult": None}, {}],
            {"not": "a list"},
        ):
            self.assertFalse(check_canaries._names_run(results, "o/r", 123), results)


class ProvenanceVerifierTests(unittest.TestCase):
    ASSET = b"release bytes"

    def canary(self) -> check_canaries.Canary:
        return check_canaries.parse_manifest(manifest())[0]

    def run_verifier(self, *, release_doc: object = None, tag_sha: object = HEAD,
                     verified: object = None, asset: bytes = ASSET) -> list[list[str]]:
        digest = "sha256:" + check_canaries.hashlib.sha256(self.ASSET).hexdigest()
        if release_doc is None:
            release_doc = {"draft": False, "immutable": True, "assets": [
                {"name": "pkg-1.2.3.tar.gz", "digest": digest},
                {"name": "pkg-1.2.3.spdx.json", "digest": "sha256:0"},
            ]}
        if verified is None:
            verified = [{"verificationResult": {"signature": {"certificate": {
                "runInvocationURI": "https://github.com/ryanduguid/example/actions/runs/123/attempts/1"
            }}}}]
        calls: list[list[str]] = []

        def run_gh(arguments: list[str]) -> str:
            calls.append(list(arguments))
            if arguments[0] == "release":
                directory = arguments[arguments.index("--dir") + 1]
                (Path(directory) / arguments[arguments.index("--pattern") + 1]).write_bytes(asset)
                return ""
            return check_canaries.json.dumps(verified)

        with mock.patch.object(check_canaries, "_gh_json", return_value=release_doc), \
                mock.patch.object(check_canaries, "_tag_commit", return_value=tag_sha), \
                mock.patch.object(check_canaries, "_run_gh", side_effect=run_gh):
            check_canaries._verify_provenance(self.canary())
        return calls

    def test_verifies_the_tarball_against_the_pinned_signer_source_and_run(self) -> None:
        calls = self.run_verifier()
        verify = calls[1]
        self.assertEqual(verify[:2], ["attestation", "verify"])
        for flag, value in (
            ("--signer-workflow", "ryanduguid/release-policy/.github/workflows/release-python.yml"),
            ("--signer-digest", SHA),
            ("--source-ref", "refs/tags/v1.2.3"),
            ("--source-digest", HEAD),
            ("--predicate-type", "https://slsa.dev/provenance/v1"),
        ):
            self.assertEqual(verify[verify.index(flag) + 1], value)

    def test_refuses_each_broken_binding(self) -> None:
        cases = {
            "published immutable": {"release_doc": {"draft": False, "immutable": False}},
            "points to": {"tag_sha": OLDER_SHA},
            "expected 1": {"release_doc": {"draft": False, "immutable": True, "assets": []}},
            "digest": {"asset": b"other bytes"},
            "names run": {"verified": [{"verificationResult": {"signature": {"certificate": {
                "runInvocationURI": "https://github.com/ryanduguid/example/actions/runs/1234/attempts/1"
            }}}}]},
        }
        for expected, overrides in cases.items():
            with self.assertRaisesRegex(RuntimeError, expected):
                self.run_verifier(**overrides)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
