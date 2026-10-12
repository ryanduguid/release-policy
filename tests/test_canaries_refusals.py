"""Manifest refusals and command paths of check_canaries the audit tests do not reach."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import check_canaries

import test_canaries as base


def _family_change(**fields: object):
    def change(document: dict) -> None:
        document["families"][0].update(fields)

    return change


def _evidence_change(**fields: object):
    def change(document: dict) -> None:
        document["families"][0]["evidence"].update(fields)

    return change


class ManifestRefusalTests(unittest.TestCase):
    def test_parse_rejects_each_malformed_field(self) -> None:
        def schema_two(document: dict) -> None:
            document["schema"] = 2

        def families_mapping(document: dict) -> None:
            document["families"] = {}

        cases = (
            (schema_two, None, "manifest schema must be 1"),
            (families_mapping, None, "manifest families must be a list"),
            (_family_change(family=""), None, r"family\[0\]\.family must be a non-empty string"),
            (_family_change(family="nightly"), None, "unsupported canary family: nightly"),
            (_family_change(repository="example"), None, "python.repository must be owner/name"),
            (_family_change(workflow_path="release.yml"), None, "python.workflow_path is not a workflow path"),
            (_family_change(policy_workflow="release.yml"), None, "python.policy_workflow is not a workflow path"),
            (_evidence_change(run_id=0), None, "python.evidence.run_id must be positive"),
            (None, "verify", "verify evidence must use main"),
        )
        for change, family, message in cases:
            with self.subTest(message=message):
                document = base.manifest(family=family or "python", ref="v1.2.3")
                if change is not None:
                    change(document)
                with self.assertRaisesRegex(ValueError, message):
                    check_canaries.parse_manifest(document)

    def write(self, directory: str, raw: bytes) -> Path:
        path = Path(directory) / "canaries.json"
        path.write_bytes(raw)
        return path

    def test_load_rejects_a_bom_invalid_text_and_missing_families(self) -> None:
        canonical = (json.dumps(base.manifest(), indent=2, sort_keys=True) + "\n").encode()
        with tempfile.TemporaryDirectory(prefix="canary-manifest-") as directory:
            for raw, message in (
                (b"\xef\xbb\xbf" + canonical, "must not contain a UTF-8 BOM"),
                (b"{\xff}", "must be valid UTF-8 JSON"),
                (canonical, "must cover"),
            ):
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        check_canaries.load_manifest(self.write(directory, raw))


class LiveAuditHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.canary = check_canaries.parse_manifest(base.manifest())[0]

    def test_a_workflow_without_one_literal_pin_is_an_audit_error(self) -> None:
        result = base.audit(base.manifest(), base.expired_fetch(), pin="no pin here",
                            verifier=base.accept, now=base.BEFORE_EXPIRY)
        self.assertTrue(any("expected one literal" in error for error in result.errors))

    def test_referenced_workflows_ignore_malformed_and_unrelated_entries(self) -> None:
        run = {"referenced_workflows": ["not a mapping", {"path": "other/repo@x", "sha": base.SHA}]}
        self.assertIsNone(check_canaries._referenced_policy_sha(run, self.canary))

    def test_a_listing_without_workflow_runs_has_no_relevant_run(self) -> None:
        self.assertIsNone(check_canaries._latest_relevant_run({"total_count": 0}, self.canary))
        self.assertIsNone(check_canaries._latest_relevant_run([], self.canary))


class MainTests(unittest.TestCase):
    def run_main(self, *arguments: str) -> tuple[int, str]:
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = check_canaries.main(list(arguments))
        return code, printed.getvalue()

    def test_offline_validation_of_the_recorded_manifest(self) -> None:
        code, output = self.run_main()
        self.assertEqual(code, 0)
        self.assertRegex(output, r"^canary manifest valid: \d+ release families\n$")

    def test_live_audit_reports_and_fails_on_errors_or_required_warnings(self) -> None:
        cases = (
            (check_canaries.CheckResult(errors=("pin drift",), warnings=(), notices=()), (), 1,
             "FAIL pin drift\n"),
            (check_canaries.CheckResult(errors=(), warnings=("stale run",), notices=()),
             ("--require-current-evidence",), 1, "WARN stale run\n"),
        )
        for result, extra, code, output in cases:
            with self.subTest(output=output):
                with mock.patch.object(check_canaries, "check_live", return_value=result):
                    self.assertEqual(self.run_main("--live", *extra), (code, output))


if __name__ == "__main__":
    unittest.main()
