from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts import check_canaries


SHA = "1" * 40
OLDER_SHA = "2" * 40
HEAD = "3" * 40


def manifest(*, current_sha: str = SHA, evidence_sha: str = SHA) -> dict[str, object]:
    return {
        "schema": 1,
        "families": [
            {
                "family": "python",
                "repository": "ryanduguid/example",
                "workflow_path": ".github/workflows/release.yml",
                "policy_workflow": ".github/workflows/release-python.yml",
                "current_policy_sha": current_sha,
                "evidence": {
                    "run_id": 123,
                    "head_sha": HEAD,
                    "ref": "v1.2.3",
                    "policy_sha": evidence_sha,
                    "started_at": "2026-08-27T00:00:00Z",
                },
            }
        ],
    }


def run_payload(*, policy_sha: str = SHA, run_id: int = 123) -> dict[str, object]:
    return {
        "id": run_id,
        "conclusion": "success",
        "event": "push",
        "head_branch": "v1.2.3",
        "head_sha": HEAD,
        "path": ".github/workflows/release.yml",
        "run_started_at": "2026-08-27T00:00:00Z",
        "referenced_workflows": [
            {
                "path": (
                    "ryanduguid/release-policy/.github/workflows/"
                    f"release-python.yml@{policy_sha}"
                ),
                "sha": policy_sha,
            }
        ],
    }


class CanaryManifestTests(unittest.TestCase):
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
