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


class YamlContractAssertions:
    def uncomment_yaml_line(self, line: str) -> str:
        single_quoted = False
        double_quoted = False
        escaped = False
        index = 0
        while index < len(line):
            character = line[index]
            if double_quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    double_quoted = False
            elif single_quoted:
                if character == "'":
                    if index + 1 < len(line) and line[index + 1] == "'":
                        index += 1
                    else:
                        single_quoted = False
            elif character == '"':
                double_quoted = True
            elif character == "'":
                single_quoted = True
            elif character == "#" and (index == 0 or line[index - 1].isspace()):
                return line[:index].rstrip()
            index += 1
        return line.rstrip()

    def mapping_entries(
        self,
        text: str,
        *,
        indent: int,
    ) -> tuple[tuple[str, str, int], ...]:
        entries: list[tuple[str, str, int]] = []
        pattern = re.compile(rf"^(?P<key>{YAML_KEY})\s*:\s*(?P<value>.*)$")
        for line_number, raw_line in enumerate(text.splitlines()):
            active = self.uncomment_yaml_line(raw_line)
            if not active.strip():
                continue
            leading = len(active) - len(active.lstrip(" "))
            if leading != indent:
                continue
            content = active[indent:]
            match = pattern.fullmatch(content)
            self.assertIsNotNone(
                match,
                f"unsupported mapping syntax at indentation {indent}: {content}",
            )
            key = match.group("key")
            if key[:1] in {"'", '"'}:
                key = key[1:-1]
            entries.append((key, match.group("value").strip(), line_number))
        return tuple(entries)

    def mapping_keys(self, text: str, *, indent: int) -> tuple[str, ...]:
        return tuple(key for key, _, _ in self.mapping_entries(text, indent=indent))

    def mapping_block(self, text: str, name: str, *, indent: int) -> str:
        matches = tuple(
            (value, line_number)
            for key, value, line_number in self.mapping_entries(text, indent=indent)
            if key == name
        )
        self.assertEqual(1, len(matches), f"mapping block is not unique: {name}")
        value, line_number = matches[0]
        self.assertEqual(
            "",
            value,
            f"mapping block must not use a flow map or alias: {name}",
        )
        lines = text.splitlines(keepends=True)
        end = len(lines)
        for index in range(line_number + 1, len(lines)):
            active = self.uncomment_yaml_line(lines[index].rstrip("\r\n"))
            if not active.strip():
                continue
            leading = len(active) - len(active.lstrip(" "))
            if leading <= indent:
                end = index
                break
        return "".join(lines[line_number + 1 : end])

    def simple_scalar(self, raw: str, *, label: str) -> str:
        self.assertTrue(raw, f"{label} must be a scalar")
        self.assertNotIn(raw[0], "*&{[", f"{label} must not use an alias or flow value")
        if raw[0] == "'":
            self.assertTrue(raw.endswith("'"), f"unterminated scalar: {label}")
            return raw[1:-1].replace("''", "'")
        if raw[0] == '"':
            self.assertTrue(raw.endswith('"'), f"unterminated scalar: {label}")
            inner = raw[1:-1]
            self.assertNotIn("\\", inner, f"escaped scalar is unsupported: {label}")
            return inner
        return raw

    def mapping_value(self, text: str, name: str, *, indent: int) -> str:
        matches = tuple(
            value
            for key, value, _ in self.mapping_entries(text, indent=indent)
            if key == name
        )
        self.assertEqual(1, len(matches), f"mapping value is not unique: {name}")
        return self.simple_scalar(matches[0], label=name)

    def permission_map(self, text: str, *, indent: int) -> dict[str, str]:
        body = self.mapping_block(text, "permissions", indent=indent)
        entries = self.mapping_entries(body, indent=indent + 2)
        keys = tuple(key for key, _, _ in entries)
        self.assertEqual(len(keys), len(set(keys)), "duplicate permission key")
        return {
            key: self.simple_scalar(value, label=f"permission {key}")
            for key, value, _ in entries
        }


class SkillWorkflowContractTests(YamlContractAssertions, unittest.TestCase):
    def yaml_key(self, name: str) -> str:
        escaped = re.escape(name)
        return rf'(?:{escaped}|\'{escaped}\'|"{escaped}")'

    def direct_core_consumer_call(self, text: str) -> re.Match[str] | None:
        return re.search(
            re.escape(
                "ryanduguid/release-policy/.github/workflows/"
                "publish-archives.yml@"
            ),
            text,
        )

    def read_workflow(self, name: str) -> str:
        path = WORKFLOWS / name
        self.assertTrue(path.is_file(), f"required workflow is missing: {name}")
        return path.read_text(encoding="utf-8")

    def needs_values(self, job: str) -> tuple[str, ...]:
        matches = tuple(
            value
            for key, value, _ in self.mapping_entries(job, indent=4)
            if key == "needs"
        )
        self.assertEqual(1, len(matches), "needs value is not unique")
        raw = matches[0]
        if raw.startswith("["):
            self.assertTrue(raw.endswith("]"), "unterminated needs flow sequence")
            values = tuple(
                self.simple_scalar(item.strip(), label="needs item")
                for item in raw[1:-1].split(",")
                if item.strip()
            )
            self.assertTrue(values, "needs flow sequence must not be empty")
            return values
        return (self.simple_scalar(raw, label="needs"),)

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
        return tuple(
            (name, self.mapping_block(body, name, indent=6))
            for name in self.mapping_keys(body, indent=6)
        )

    def with_keys(self, job: str) -> tuple[str, ...]:
        body = self.mapping_block(job, "with", indent=4)
        return self.mapping_keys(body, indent=6)

    def assert_release_dag_contract(self, workflow: str) -> None:
        jobs = self.mapping_block(workflow, "jobs", indent=0)
        self.assertEqual(
            self.mapping_keys(jobs, indent=2),
            ("guard", "verify", "publish"),
        )
        guard = self.job_block(workflow, "guard")
        verify = self.job_block(workflow, "verify")
        publish = self.job_block(workflow, "publish")

        self.assertEqual(
            self.mapping_keys(guard, indent=4),
            ("timeout-minutes", "name", "runs-on", "permissions", "steps"),
        )
        self.assertEqual(
            self.mapping_keys(verify, indent=4),
            ("needs", "permissions", "uses", "with"),
        )
        self.assertEqual(
            self.mapping_keys(publish, indent=4),
            ("needs", "permissions", "uses", "with"),
        )
        self.assertEqual(self.needs_values(verify), ("guard",))
        self.assertEqual(self.needs_values(publish), ("guard", "verify"))

    def assert_release_publication_contract(self, workflow: str) -> None:
        publish = self.job_block(workflow, "publish")

        self.assertEqual(
            self.mapping_keys(publish, indent=4),
            ("needs", "permissions", "uses", "with"),
        )
        self.assertEqual(self.needs_values(publish), ("guard", "verify"))
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

        self.assert_release_dag_contract(workflow)

    def test_release_dag_contract_rejects_extra_jobs_and_wrong_active_needs(self) -> None:
        workflow = self.read_workflow("release-skills.yml")
        extra_job = workflow.replace(
            "jobs:\n  guard:",
            "jobs:\n"
            "  bypass:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: true\n\n"
            "  guard:",
            1,
        )
        wrong_needs = workflow.replace(
            "    needs: [guard, verify]\n",
            "    needs: bypass\n    # needs: [guard, verify]\n",
            1,
        )
        flow_job = workflow.replace(
            "  verify:\n"
            "    needs: guard\n"
            "    permissions:\n"
            "      contents: read\n"
            "    uses: ./.github/workflows/verify-skills.yml\n"
            "    with:\n"
            "      skills-verification-mode: ${{ inputs.skills-verification-mode }}\n"
            "      version-file: ${{ inputs.version-file }}\n",
            "  verify: {needs: guard, permissions: {contents: read}, uses: "
            "./.github/workflows/verify-skills.yml, with: "
            '{skills-verification-mode: "${{ inputs.skills-verification-mode }}", '
            'version-file: "${{ inputs.version-file }}"}}\n',
            1,
        )
        anchored_job = workflow.replace("  guard:\n", "  guard: &guard_job\n", 1)
        aliased_job = anchored_job[: anchored_job.index("  publish:")] + (
            "  publish: *guard_job\n"
        )

        # Catches an unapproved fourth job in the closed release DAG.
        with self.subTest(mutation="extra job"), self.assertRaises(AssertionError):
            self.assert_release_dag_contract(extra_job)
        # Catches a publication job whose only active dependency bypasses the
        # guard and verifier while the approved dependency survives as a comment.
        with self.subTest(mutation="wrong active needs"), self.assertRaises(
            AssertionError
        ):
            self.assert_release_dag_contract(wrong_needs)
        # Catches a contract-owned called job supplied as an inline flow map.
        with self.subTest(mutation="flow-map job"), self.assertRaises(AssertionError):
            self.assert_release_dag_contract(flow_job)
        # Catches a contract-owned called job supplied through an alias.
        with self.subTest(mutation="aliased job"), self.assertRaises(AssertionError):
            self.assert_release_dag_contract(aliased_job)

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
        workflow = self.read_workflow("release-skills.yml")

        self.assert_release_publication_contract(workflow)

    def test_release_publication_contract_rejects_quoted_flow_and_alias_permissions(
        self,
    ) -> None:
        workflow = self.read_workflow("release-skills.yml")
        quoted_permission = workflow.replace(
            "      id-token: write\n    uses: ./.github/workflows/publish-archives.yml",
            '      id-token: write\n      "packages": write\n'
            "    uses: ./.github/workflows/publish-archives.yml",
            1,
        )
        flow_permission = workflow.replace(
            "    permissions:\n"
            "      attestations: write\n"
            "      contents: write\n"
            "      id-token: write\n",
            "    permissions: {attestations: write, contents: write, "
            "id-token: write}\n",
            1,
        )
        aliased_permission = workflow.replace(
            "permissions:\n  contents: read\n",
            "permissions: &publish_permissions\n"
            "  attestations: write\n"
            "  contents: write\n"
            "  id-token: write\n",
            1,
        ).replace(
            "    permissions:\n"
            "      attestations: write\n"
            "      contents: write\n"
            "      id-token: write\n",
            "    permissions: *publish_permissions\n",
            1,
        )

        # Catches an extra write permission hidden behind a quoted YAML key.
        with self.assertRaises(AssertionError):
            self.assert_release_publication_contract(quoted_permission)
        # Catches a privileged permission block supplied as an inline flow map.
        with self.assertRaises(AssertionError):
            self.assert_release_publication_contract(flow_permission)
        # Catches a privileged permission block supplied through an alias.
        with self.assertRaises(AssertionError):
            self.assert_release_publication_contract(aliased_permission)

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
            'name: Flow example\non: push\njobs: {release: {uses: "'
            'ryanduguid/release-policy/.github/workflows/'
            'publish-archives.yml@<full-40-char-commit-sha>"}}',
            'name: Alias example\non: push\nenv:\n  CORE: &core "'
            'ryanduguid/release-policy/.github/workflows/'
            'publish-archives.yml@<full-40-char-commit-sha>"\n'
            "jobs:\n  release:\n    uses: *core",
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
