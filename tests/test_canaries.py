from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

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

    def test_live_legacy_release_and_verify_select_only_their_expected_refs(self) -> None:
        for family in ("archive", "python", "skills", "verify"):
            with self.subTest(family=family):
                ref = "main" if family == "verify" else "v1.2.3"
                recorded = run_payload(family=family, ref=ref)
                policy_workflow = (
                    "verify-skills.yml" if family == "verify" else f"release-{family}.yml"
                )

                def fetch_json(endpoint: str) -> object:
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
        self.assertEqual(len(requests), 3)

    def test_detects_stale_current_pin_and_newer_success(self) -> None:
        def fetch_json(endpoint: str) -> object:
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
        self.assertIn("python scripts/check_canaries.py --live", workflow)


if __name__ == "__main__":
    unittest.main()
