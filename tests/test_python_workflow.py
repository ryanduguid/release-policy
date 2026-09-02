from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-python.yml"


def job_block(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    if match is None:
        raise AssertionError(f"job {name!r} is missing")
    return match.group(0)


class PythonWorkflowBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.publication_script = (
            ROOT / "scripts" / "publish_python.sh"
        ).read_text(encoding="utf-8")

    def test_exposes_only_closed_release_inputs_with_root_compatible_defaults(self) -> None:
        self.assertNotIn("version-command", self.workflow)
        self.assertNotIn("bash -c", self.workflow)
        self.assertIn("source-directory:", self.workflow)
        self.assertRegex(
            self.workflow,
            r"(?ms)^      source-directory:\n.*?^        default: \.$",
        )
        self.assertIn("tag-prefix:", self.workflow)
        self.assertRegex(
            self.workflow,
            r'(?ms)^      tag-prefix:\n.*?^        default: ""$',
        )
        self.assertIn("version-parser:", self.workflow)
        self.assertIn("default: pyproject", self.workflow)
        self.assertIn("version-file:", self.workflow)
        self.assertIn("default: pyproject.toml", self.workflow)

    def test_component_inputs_are_validated_before_package_local_use(self) -> None:
        test = job_block(self.workflow, "test")
        build = job_block(self.workflow, "build")
        publish = job_block(self.workflow, "publish")

        for job in (test, build, publish):
            validation = job.index("Validate the component release inputs")
            first_component_path = job.index("steps.inputs.outputs.source-path")
            self.assertLess(validation, first_component_path)
            self.assertIn(
                'SOURCE_DIRECTORY: ${{ inputs.source-directory }}',
                job,
            )
            self.assertIn('TAG_PREFIX: ${{ inputs.tag-prefix }}', job)
            self.assertIn('$GITHUB_WORKSPACE/policy/scripts/gates.sh', job)

        self.assertIn(
            "working-directory: ${{ steps.inputs.outputs.source-path }}",
            test,
        )
        self.assertIn(
            "working-directory: ${{ steps.inputs.outputs.source-path }}",
            build,
        )
        self.assertNotIn(". ../policy/scripts/gates.sh", self.workflow)
        self.assertNotIn("python ../policy/scripts/", self.workflow)

    def test_namespaced_full_tag_never_becomes_a_candidate_filesystem_name(self) -> None:
        build = job_block(self.workflow, "build")
        publish = job_block(self.workflow, "publish")

        self.assertIn(
            "name: python-${{ steps.release.outputs.artifact-tag }}-",
            build,
        )
        self.assertIn(
            "CANDIDATE_NAME: python-${{ needs.build.outputs.artifact-tag }}-",
            publish,
        )
        self.assertNotIn(
            "name: python-${{ steps.release.outputs.tag }}-",
            build,
        )
        self.assertIn('--tag "${{ steps.release.outputs.tag }}"', build)
        self.assertIn('--source-ref "refs/tags/$tag"', publish)
        self.assertIn("SOURCE_PATH: ${{ steps.inputs.outputs.source-path }}", publish)
        self.assertIn('$source_path/RELEASE_NOTES.md', self.publication_script)

    def test_build_is_read_only_and_publish_is_the_only_authorised_job(self) -> None:
        test = job_block(self.workflow, "test")
        build = job_block(self.workflow, "build")
        publish = job_block(self.workflow, "publish")

        self.assertIn("permissions:\n      contents: read", test)
        self.assertIn("uv run --locked --extra dev --python 3.12 pytest", test)
        for forbidden in ("contents: write", "attestations: write", "id-token: write"):
            self.assertNotIn(forbidden, test)

        self.assertIn("needs: test", build)
        self.assertIn("permissions:\n      contents: read", build)
        for forbidden in ("contents: write", "attestations: write", "id-token: write"):
            self.assertNotIn(forbidden, build)
        self.assertIn("python -m build", build)
        self.assertNotIn("pytest", build)

        self.assertIn("needs: build", publish)
        for required in (
            "actions: read",
            "attestations: write",
            "contents: write",
            "id-token: write",
        ):
            self.assertIn(required, publish)
        for forbidden in ("uv run", "python -m build", "pytest"):
            self.assertNotIn(forbidden, publish)

        self.assertEqual(
            self.workflow.count("group: release-${{ github.repository }}-${{ github.ref }}"),
            1,
        )
        self.assertRegex(
            self.workflow,
            r"(?m)^concurrency:\n  group: release-\$\{\{ github\.repository \}\}-"
            r"\$\{\{ github\.ref \}\}\n  cancel-in-progress: false$",
        )

    def test_release_dependencies_never_restore_shared_caches(self) -> None:
        self.assertNotIn("enable-cache: true", self.workflow)
        self.assertEqual(self.workflow.count("enable-cache: false"), 2)

    def test_candidate_is_bound_by_immutable_artifact_id_digest_run_and_source(self) -> None:
        build = job_block(self.workflow, "build")
        publish = job_block(self.workflow, "publish")

        self.assertIn("id: candidate", build)
        self.assertIn("artifact-id", build)
        self.assertIn("artifact-digest", build)
        self.assertIn("overwrite: false", build)
        self.assertIn("release-manifest.json", build)
        self.assertIn("SHA256SUMS", build)

        self.assertIn("repos/$GITHUB_REPOSITORY/actions/artifacts/$CANDIDATE_ID", publish)
        self.assertIn(".workflow_run.id == $run_id", publish)
        self.assertIn(".workflow_run.head_sha == $source_sha", publish)
        self.assertIn(".digest == $digest", publish)
        self.assertIn("artifact-ids: ${{ needs.build.outputs.candidate-id }}", publish)
        self.assertIn("verify-candidate", publish)

    def test_consumer_policy_and_candidate_use_isolated_sibling_directories(self) -> None:
        test = job_block(self.workflow, "test")
        build = job_block(self.workflow, "build")
        publish = job_block(self.workflow, "publish")

        for job in (test, build, publish):
            self.assertIn("path: source", job)
            self.assertIn("path: policy", job)
            self.assertNotIn("path: .release-policy", job)
        self.assertNotIn(".release-policy/", self.workflow)
        self.assertIn("working-directory: source", test)
        self.assertIn("working-directory: source", build)
        self.assertIn("path: candidate", publish)
        self.assertIn("--directory candidate", publish)
        self.assertIn("SOURCE_PATH: ${{ steps.inputs.outputs.source-path }}", publish)
        self.assertIn('$source_path/RELEASE_NOTES.md', self.publication_script)

    def test_attestation_and_release_checks_bind_signer_source_ref_and_assets(self) -> None:
        publish = job_block(self.workflow, "publish")
        policy_code = publish + self.publication_script

        for required in (
            "--source-digest",
            "--source-ref",
            "--signer-workflow",
            "--signer-digest",
            "gate_main_matches",
            "gate_no_existing_release",
            ".immutable == true",
            "gh release verify-asset",
        ):
            self.assertIn(required, policy_code)
        self.assertIn(
            'signer="ryanduguid/release-policy/.github/workflows/release-python.yml"',
            publish,
        )

    def test_draft_creation_and_cleanup_use_only_the_current_numeric_release_id(self) -> None:
        publication_script = self.publication_script

        self.assertNotIn("gh release create", publication_script)
        self.assertNotIn("gh release upload", publication_script)
        self.assertIn("gh api --method POST", publication_script)
        self.assertIn("release_id=", publication_script)
        self.assertIn(
            "repos/$GITHUB_REPOSITORY/releases/$release_id", publication_script
        )
        self.assertIn(
            'upload_url="https://uploads.github.com/repos/$GITHUB_REPOSITORY/'
            'releases/$release_id/assets"',
            publication_script,
        )
        self.assertNotIn(".upload_url |", publication_script)
        self.assertIn("cleanup_current_draft", publication_script)
        self.assertIn(".id == $release_id", publication_script)
        self.assertIn(".draft == true", publication_script)
        self.assertIn("gh api --method DELETE", publication_script)
        self.assertIn("published=true", publication_script)

    def test_publish_delegates_the_release_state_machine_to_policy_code(self) -> None:
        publish = job_block(self.workflow, "publish")
        script = ROOT / "scripts" / "publish_python.sh"

        self.assertTrue(script.is_file(), "the trusted Python publication script is missing")
        script_text = script.read_text(encoding="utf-8")
        self.assertIn('bash "$GITHUB_WORKSPACE/policy/scripts/publish_python.sh"', publish)
        self.assertNotIn("cleanup_current_draft()", publish)
        self.assertIn("cleanup_current_draft()", script_text)
        self.assertNotIn("${{", script_text)
        self.assertIn('source_path="$5"', script_text)


if __name__ == "__main__":
    unittest.main()
