from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"
YAML_KEY = r'(?:[A-Za-z0-9_-]+|\'[A-Za-z0-9_-]+\'|"[A-Za-z0-9_-]+")'


class SkillWorkflowContractTests(unittest.TestCase):
    def yaml_key(self, name: str) -> str:
        escaped = re.escape(name)
        return rf'(?:{escaped}|\'{escaped}\'|"{escaped}")'

    def direct_core_consumer_call(self, text: str) -> re.Match[str] | None:
        return re.search(
            rf"(?mi)^\s*(?:-\s+)?{self.yaml_key('uses')}\s*:\s*"
            r"(?P<quote>[\"']?)"
            r"ryanduguid/release-policy/\.github/workflows/"
            r"publish-archives\.yml@[^\"'#\s]+"
            r"(?P=quote)\s*(?:#.*)?$",
            text,
        )

    def read_workflow(self, name: str) -> str:
        path = WORKFLOWS / name
        self.assertTrue(path.is_file(), f"required workflow is missing: {name}")
        return path.read_text(encoding="utf-8")

    def mapping_keys(self, text: str, *, indent: int) -> tuple[str, ...]:
        prefix = " " * indent
        pattern = re.compile(
            rf"(?m)^{prefix}(?:-\s+)?"
            r'(?:(?P<plain>[A-Za-z0-9_-]+)|\'(?P<single>[A-Za-z0-9_-]+)\''
            r'|"(?P<double>[A-Za-z0-9_-]+)")\s*:',
        )
        return tuple(
            next(
                value
                for value in match.group("plain", "single", "double")
                if value is not None
            )
            for match in pattern.finditer(text)
        )

    def mapping_block(self, text: str, name: str, *, indent: int) -> str:
        prefix = " " * indent
        match = re.search(
            rf"(?ms)^{prefix}(?:-\s+)?{self.yaml_key(name)}\s*:[^\n]*\n"
            rf"(.*?)(?=^{prefix}(?:-\s+)?{YAML_KEY}\s*:|\Z)",
            text,
        )
        self.assertIsNotNone(match, f"mapping block is missing: {name}")
        return match.group(1)

    def assert_top_level_skeleton(self, workflow: str) -> None:
        self.assertEqual(
            self.mapping_keys(workflow, indent=0),
            ("name", "on", "permissions", "jobs"),
        )
        trigger = self.mapping_block(workflow, "on", indent=0)
        self.assertEqual(self.mapping_keys(trigger, indent=2), ("workflow_call",))
        workflow_call = self.mapping_block(trigger, "workflow_call", indent=2)
        self.assertEqual(self.mapping_keys(workflow_call, indent=4), ("inputs",))

    def job_block(self, workflow: str, name: str) -> str:
        self.assert_top_level_skeleton(workflow)
        jobs = self.mapping_block(workflow, "jobs", indent=0)
        return self.mapping_block(jobs, name, indent=2)

    def step_block(self, job: str, name: str) -> str:
        steps = self.mapping_block(job, "steps", indent=4)
        match = re.search(
            rf"(?ms)^      -\s+{self.yaml_key('name')}\s*: "
            rf"{re.escape(name)}\n"
            rf"(.*?)(?=^      -\s+{YAML_KEY}\s*:|\Z)",
            steps,
        )
        self.assertIsNotNone(match, f"step is missing: {name}")
        return match.group(1)

    def folded_run_lines(self, step: str) -> tuple[str, ...]:
        match = re.search(
            r"(?m)^        run: >-\n((?:^          .*\n?)+)",
            step,
        )
        self.assertIsNotNone(match, "folded run command is missing")
        return tuple(line[10:] for line in match.group(1).splitlines())

    def workflow_inputs(self, workflow: str) -> tuple[tuple[str, str], ...]:
        self.assert_top_level_skeleton(workflow)
        trigger = self.mapping_block(workflow, "on", indent=0)
        workflow_call = self.mapping_block(trigger, "workflow_call", indent=2)
        body = self.mapping_block(workflow_call, "inputs", indent=4)
        starts = list(re.finditer(r"(?m)^      ([a-z0-9-]+):\n", body))
        return tuple(
            (
                start.group(1),
                body[start.end() : starts[index + 1].start()]
                if index + 1 < len(starts)
                else body[start.end() :],
            )
            for index, start in enumerate(starts)
        )

    def permission_map(self, text: str, *, indent: int) -> dict[str, str]:
        prefix = " " * indent
        match = re.search(
            rf"(?m)^{prefix}permissions:\n((?:^{prefix}  [a-z-]+: [a-z]+\n)+)",
            text,
        )
        self.assertIsNotNone(match, "permissions map is missing")
        return dict(
            re.findall(rf"(?m)^{prefix}  ([a-z-]+): ([a-z]+)$", match.group(1))
        )

    def with_keys(self, job: str) -> tuple[str, ...]:
        match = re.search(
            r"(?m)^    with:\n((?:^      [a-z0-9-]+:.*\n?)+)",
            job,
        )
        self.assertIsNotNone(match, "called workflow inputs are missing")
        return tuple(re.findall(r"(?m)^      ([a-z0-9-]+):", match.group(1)))

    def test_verification_workflow_has_closed_interface_and_permissions(self) -> None:
        workflow = self.read_workflow("verify-skills.yml")
        inputs = dict(self.workflow_inputs(workflow))

        self.assertEqual(
            tuple(inputs),
            ("skills-verification-mode", "version-file"),
        )
        self.assertRegex(inputs["skills-verification-mode"], r"(?m)^        required: true$")
        self.assertRegex(inputs["skills-verification-mode"], r"(?m)^        type: string$")
        self.assertRegex(inputs["version-file"], r"(?m)^        required: false$")
        self.assertRegex(inputs["version-file"], r"(?m)^        type: string$")
        self.assertRegex(inputs["version-file"], r"(?m)^        default: VERSION$")
        self.assertEqual(self.permission_map(workflow, indent=0), {"contents": "read"})

        verify = self.job_block(workflow, "verify")
        self.assertEqual(self.permission_map(verify, indent=4), {"contents": "read"})
        self.assertRegex(verify, r"(?m)^    timeout-minutes: 15$")

    def test_reusable_workflows_keep_the_required_parent_skeleton(self) -> None:
        for name in ("verify-skills.yml", "release-skills.yml"):
            with self.subTest(workflow=name):
                workflow = self.read_workflow(name)
                self.assert_top_level_skeleton(workflow)
                self.workflow_inputs(workflow)

    def test_verification_workflow_uses_isolated_sibling_checkouts_at_full_sha(self) -> None:
        workflow = self.read_workflow("verify-skills.yml")
        verify = self.job_block(workflow, "verify")

        self.assertEqual(verify.count(f"uses: actions/checkout@{CHECKOUT_SHA}"), 2)
        self.assertRegex(
            verify,
            rf"(?ms)- name: Check out consumer source\n"
            rf"        uses: actions/checkout@{CHECKOUT_SHA}.*?"
            r"^          path: consumer\n"
            r"^          persist-credentials: false$",
        )
        self.assertRegex(
            verify,
            rf"(?ms)- name: Check out release-policy at the calling pin\n"
            rf"        uses: actions/checkout@{CHECKOUT_SHA}.*?"
            r"^          repository: ryanduguid/release-policy\n"
            r"^          ref: \$\{\{ job\.workflow_sha \}\}\n"
            r"^          path: policy\n"
            r"^          persist-credentials: false$",
        )
        self.assertIn('[[ "$MODULE_SHA" =~ ^[0-9a-f]{40}$ ]]', verify)
        self.assertIn('test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"', verify)

    def test_verification_workflow_runs_the_fixed_python_312_verifier(self) -> None:
        verify = self.job_block(self.read_workflow("verify-skills.yml"), "verify")

        self.assertIn(f"uses: actions/setup-python@{SETUP_PYTHON_SHA}", verify)
        self.assertRegex(verify, r'(?m)^          python-version: "3\.12"$')
        self.assertRegex(verify, r"(?m)^        working-directory: consumer$")
        self.assertIn("python ../policy/scripts/verify_skills.py verify", verify)
        self.assertIn('--mode "$SKILLS_VERIFICATION_MODE"', verify)
        self.assertIn('--version-file "$VERSION_FILE"', verify)

    def test_release_workflow_has_closed_interface_without_secrets_or_outputs(self) -> None:
        workflow = self.read_workflow("release-skills.yml")
        inputs = dict(self.workflow_inputs(workflow))

        self.assertEqual(
            tuple(inputs),
            ("artifact-stem", "version-file", "skills-verification-mode"),
        )
        for name in ("artifact-stem", "skills-verification-mode"):
            self.assertRegex(inputs[name], r"(?m)^        required: true$")
            self.assertRegex(inputs[name], r"(?m)^        type: string$")
        self.assertRegex(inputs["version-file"], r"(?m)^        required: false$")
        self.assertRegex(inputs["version-file"], r"(?m)^        type: string$")
        self.assertRegex(inputs["version-file"], r"(?m)^        default: VERSION$")
        self.assertEqual(self.permission_map(workflow, indent=0), {"contents": "read"})

    def test_release_guard_always_runs_and_fails_the_frozen_tag(self) -> None:
        workflow = self.read_workflow("release-skills.yml")
        guard = self.job_block(workflow, "guard")
        guard_step = self.step_block(guard, "Guard the selected mode and frozen tag")

        self.assertNotRegex(guard, r"(?m)^    if:")
        self.assertNotRegex(
            guard,
            rf"(?m)^    {self.yaml_key('continue-on-error')}\s*:",
        )
        self.assertEqual(
            self.mapping_keys(guard, indent=4),
            ("timeout-minutes", "name", "runs-on", "permissions", "steps"),
        )
        self.assertRegex(guard, r"(?m)^    timeout-minutes: 5$")
        self.assertEqual(self.permission_map(guard, indent=4), {"contents": "read"})
        self.assertEqual(self.mapping_keys(guard_step, indent=8), ("env", "run"))
        self.assertNotRegex(
            guard_step,
            rf"(?m)^        {self.yaml_key('continue-on-error')}\s*:",
        )
        self.assertNotIn("|| true", guard_step)
        self.assertEqual(
            self.folded_run_lines(guard_step),
            (
                "python policy/scripts/verify_skills.py guard-release",
                '--mode "$SKILLS_VERIFICATION_MODE"',
                '--tag "$RELEASE_TAG"',
            ),
        )
        self.assertIn("RELEASE_TAG: ${{ github.ref_name }}", guard_step)
        self.assertIn(
            "SKILLS_VERIFICATION_MODE: ${{ inputs.skills-verification-mode }}",
            guard_step,
        )
        self.assertIn("ref: ${{ job.workflow_sha }}", guard)
        self.assertIn('[[ "$MODULE_SHA" =~ ^[0-9a-f]{40}$ ]]', guard)
        self.assertIn('test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"', guard)

        frozen = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "verify_skills.py"),
                "guard-release",
                "--mode",
                "subcontractor-accounting-v1",
                "--tag",
                "v0.1.0",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(frozen.returncode, 0)
        self.assertIn("v0.1.0 is frozen", frozen.stderr)

    def test_release_dag_cannot_skip_a_failing_guard(self) -> None:
        workflow = self.read_workflow("release-skills.yml")
        guard = self.job_block(workflow, "guard")
        verify = self.job_block(workflow, "verify")
        publish = self.job_block(workflow, "publish")

        self.assertNotRegex(guard, r"(?m)^    if:")
        self.assertRegex(verify, r"(?m)^    needs: guard$")
        self.assertNotRegex(verify, r"(?m)^    if:")
        self.assertRegex(publish, r"(?m)^    needs: \[guard, verify\]$")
        self.assertNotRegex(publish, r"(?m)^    if:")

    def test_release_verification_job_has_only_read_permission_and_verifier_inputs(self) -> None:
        verify = self.job_block(self.read_workflow("release-skills.yml"), "verify")

        self.assertEqual(
            self.mapping_keys(verify, indent=4),
            ("needs", "permissions", "uses", "with"),
        )
        self.assertEqual(self.permission_map(verify, indent=4), {"contents": "read"})
        self.assertRegex(
            verify,
            r"(?m)^    uses: \./\.github/workflows/verify-skills\.yml$",
        )
        self.assertEqual(
            self.with_keys(verify),
            ("skills-verification-mode", "version-file"),
        )

    def test_release_publication_job_has_exact_privileges_dependencies_and_inputs(self) -> None:
        publish = self.job_block(self.read_workflow("release-skills.yml"), "publish")

        self.assertEqual(
            self.mapping_keys(publish, indent=4),
            ("needs", "permissions", "uses", "with"),
        )
        self.assertRegex(publish, r"(?m)^    needs: \[guard, verify\]$")
        self.assertEqual(
            self.permission_map(publish, indent=4),
            {
                "attestations": "write",
                "contents": "write",
                "id-token": "write",
            },
        )
        self.assertRegex(
            publish,
            r"(?m)^    uses: \./\.github/workflows/publish-archives\.yml$",
        )
        self.assertEqual(self.with_keys(publish), ("artifact-stem", "version-file"))

    def test_adapters_forbid_state_transfer_open_commands_and_custom_outputs(self) -> None:
        workflows = (
            self.read_workflow("verify-skills.yml"),
            self.read_workflow("release-skills.yml"),
        )
        forbidden = (
            "secrets: inherit",
            "secrets:",
            "outputs:",
            "GITHUB_OUTPUT",
            "needs.guard.outputs",
            "needs.verify.outputs",
            "command:",
            "release-notes",
            "builder",
            "asset-glob",
        )

        for workflow in workflows:
            for fragment in forbidden:
                with self.subTest(fragment=fragment):
                    self.assertNotIn(fragment, workflow)
            self.assertNotRegex(
                workflow,
                rf"(?m)^\s*(?:-\s+)?{self.yaml_key('cache')}\s*:",
            )
            self.assertNotRegex(
                workflow,
                rf"(?mi)^\s*(?:-\s+)?{self.yaml_key('uses')}\s*:\s*"
                r"[\"']?actions/"
                r"(?:cache|download-artifact|upload-artifact)(?:/[^@\s]+)?@",
            )

    def test_supported_adapters_call_the_same_commit_core_without_direct_consumer_use(self) -> None:
        archive_adapter = self.read_workflow("release-archive.yml")
        skill_adapter = self.read_workflow("release-skills.yml")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        forbidden_consumer_examples = (
            "uses: ryanduguid/release-policy/.github/workflows/"
            "publish-archives.yml@<full-40-char-commit-sha>",
            "  'uses' : 'ryanduguid/release-policy/.github/workflows/"
            "publish-archives.yml@<full-40-char-commit-sha>'",
            '    "uses": "ryanduguid/release-policy/.github/workflows/'
            'publish-archives.yml@<full-40-char-commit-sha>"',
        )
        for example in forbidden_consumer_examples:
            with self.subTest(forbidden=example):
                self.assertIsNotNone(self.direct_core_consumer_call(example))

        supported_consumer_examples = (
            "uses: ryanduguid/release-policy/.github/workflows/"
            "verify-skills.yml@<full-40-char-commit-sha>",
            "'uses': 'ryanduguid/release-policy/.github/workflows/"
            "release-skills.yml@<full-40-char-commit-sha>'",
        )
        for example in supported_consumer_examples:
            with self.subTest(supported=example):
                self.assertIsNone(self.direct_core_consumer_call(example))

        relative_call = "uses: ./.github/workflows/publish-archives.yml"
        self.assertEqual(archive_adapter.count(relative_call), 1)
        self.assertEqual(skill_adapter.count(relative_call), 1)
        self.assertIsNone(self.direct_core_consumer_call(readme))


if __name__ == "__main__":
    unittest.main()
