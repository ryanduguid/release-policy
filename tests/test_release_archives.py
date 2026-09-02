from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_release_archives as release_archives  # noqa: E402
import find_created_draft_release as draft_release  # noqa: E402
from tests.test_skill_workflows import YAML_KEY, YamlContractAssertions  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zip_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            item.filename: archive.read(item.filename)
            for item in archive.infolist()
            if not item.is_dir()
        }


def _tar_files(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for item in archive.getmembers():
            if not item.isfile():
                continue
            stream = archive.extractfile(item)
            if stream is None:
                raise AssertionError(f"could not read {item.name}")
            files[item.name] = stream.read()
    return files


class ReleaseArchiveBuilderTests(unittest.TestCase):
    def test_parser_defaults_to_the_repository_root(self) -> None:
        arguments = release_archives._parser().parse_args(
            ["--commit", "HEAD", "--prefix", "example/", "--output-base", "dist/example"]
        )

        self.assertEqual(".", arguments.source_directory)

    def test_builder_pins_git_conversion_and_timezone(self) -> None:
        with TemporaryDirectory() as temporary, mock.patch.object(
            release_archives.subprocess,
            "run",
        ) as run:
            run.side_effect = (
                subprocess.CompletedProcess(
                    ["git", "show"], 0, stdout="1234567890\n", stderr=""
                ),
                subprocess.CompletedProcess(["git", "archive"], 0),
                subprocess.CompletedProcess(["git", "archive"], 0),
            )
            outputs = release_archives.build_release_archives(
                commit="deadbeef",
                prefix="example-1.2.3/",
                output_base=Path(temporary) / "dist" / "example-1.2.3",
                cwd=ROOT,
            )

        self.assertEqual(3, run.call_count)
        self.assertEqual(
            ("git", "show", "-s", "--format=%ct", "deadbeef^{commit}"),
            run.call_args_list[0].args[0],
        )
        self.assertEqual(
            ("example-1.2.3.zip", "example-1.2.3.tar.gz"),
            tuple(path.name for path in outputs),
        )
        for call in run.call_args_list[1:]:
            self.assertEqual(
                (
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.eol=lf",
                    "archive",
                ),
                call.args[0][:6],
            )
            self.assertEqual("UTC", call.kwargs["env"]["TZ"])
            self.assertEqual(ROOT, call.kwargs["cwd"])
            self.assertTrue(call.kwargs["check"])
            self.assertIn("--mtime=@1234567890", call.args[0])

    def test_builder_rejects_a_non_numeric_commit_timestamp(self) -> None:
        with TemporaryDirectory() as temporary, mock.patch.object(
            release_archives.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["git", "show"], 0, stdout="not-a-timestamp\n", stderr=""
            ),
        ), self.assertRaisesRegex(ValueError, "timestamp"):
            release_archives.build_release_archives(
                commit="deadbeef",
                prefix="example-1.2.3/",
                output_base=Path(temporary) / "dist" / "example-1.2.3",
                cwd=ROOT,
            )

    def test_repeated_archives_are_identical_and_formats_agree(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(os.environ, {"TZ": "Australia/Sydney"}):
                first = release_archives.build_release_archives(
                    commit="HEAD",
                    prefix="release-test/",
                    output_base=root / "first" / "release-test",
                    cwd=ROOT,
                )
            with mock.patch.dict(os.environ, {"TZ": "Pacific/Auckland"}):
                second = release_archives.build_release_archives(
                    commit="HEAD",
                    prefix="release-test/",
                    output_base=root / "second" / "release-test",
                    cwd=ROOT,
                )

            self.assertEqual(
                tuple(_sha256(path) for path in first),
                tuple(_sha256(path) for path in second),
            )
            self.assertEqual(_zip_files(first[0]), _tar_files(first[1]))
            self.assertTrue(
                all(name.startswith("release-test/") for name in _zip_files(first[0]))
            )

    def test_component_archive_contains_only_the_selected_tracked_tree(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Policy Test"],
                cwd=root,
                check=True,
            )
            (root / "root.txt").write_text("root\n", encoding="utf-8")
            component = root / "packages" / "example-tool"
            component.mkdir(parents=True)
            (component / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            (component / "source.txt").write_text("component\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "fixture"],
                cwd=root,
                check=True,
            )

            outputs = release_archives.build_release_archives(
                commit="HEAD",
                prefix="example-tool-1.2.3/",
                source_directory="packages/example-tool",
                output_base=root / "dist" / "example-tool-1.2.3",
                cwd=root,
            )

            expected = {
                "example-tool-1.2.3/VERSION": b"1.2.3\n",
                "example-tool-1.2.3/source.txt": b"component\n",
            }
            self.assertEqual(expected, _zip_files(outputs[0]))
            self.assertEqual(expected, _tar_files(outputs[1]))

    def test_component_archive_rejects_unsafe_untracked_file_and_symlink_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Policy Test"],
                cwd=root,
                check=True,
            )
            tracked = root / "packages" / "tracked"
            tracked.mkdir(parents=True)
            (tracked / "source.txt").write_text("tracked\n", encoding="utf-8")
            (root / "tracked-file.txt").write_text("file\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "fixture"],
                cwd=root,
                check=True,
            )
            untracked = root / "packages" / "untracked"
            untracked.mkdir()
            (untracked / "source.txt").write_text("untracked\n", encoding="utf-8")

            cases = (
                "",
                str(root.resolve()),
                "../escape",
                "packages\\tracked",
                "packages/untracked",
                "tracked-file.txt",
            )
            for index, source_directory in enumerate(cases):
                with self.subTest(source_directory=source_directory), self.assertRaises(
                    ValueError
                ):
                    release_archives.build_release_archives(
                        commit="HEAD",
                        prefix="safe/",
                        source_directory=source_directory,
                        output_base=root / "dist" / f"unsafe-{index}",
                        cwd=root,
                    )

            link = root / "packages" / "linked"
            try:
                link.symlink_to(tracked, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            with self.assertRaises(ValueError):
                release_archives.build_release_archives(
                    commit="HEAD",
                    prefix="safe/",
                    source_directory="packages/linked",
                    output_base=root / "dist" / "linked",
                    cwd=root,
                )

    def test_builder_refuses_unsafe_prefixes_and_existing_outputs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for prefix in ("absolute", "/absolute/", "../escape/", "safe/../escape/"):
                with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                    release_archives.build_release_archives(
                        commit="HEAD",
                        prefix=prefix,
                        output_base=root / "archive",
                        cwd=ROOT,
                    )

            existing = root / "existing.zip"
            existing.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                release_archives.build_release_archives(
                    commit="HEAD",
                    prefix="safe/",
                    output_base=root / "existing",
                    cwd=ROOT,
                )
            self.assertEqual(b"keep", existing.read_bytes())

    def test_builder_rejects_an_output_directory_symlinked_outside_the_checkout(self) -> None:
        with (
            TemporaryDirectory(dir=ROOT) as consumer_temporary,
            TemporaryDirectory() as outside_temporary,
        ):
            consumer = Path(consumer_temporary)
            outside = Path(outside_temporary)
            output_link = consumer / "dist"
            try:
                output_link.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            with self.assertRaises(ValueError):
                release_archives.build_release_archives(
                    commit="HEAD",
                    prefix="safe/",
                    output_base=output_link / "safe",
                    cwd=ROOT,
                )
            self.assertEqual([], list(outside.iterdir()))


class ReleaseArchiveWorkflowTests(YamlContractAssertions, unittest.TestCase):
    @staticmethod
    def named_step(workflow: str, name: str) -> str:
        marker = f"      - name: {name}\n"
        start = workflow.index(marker)
        end = workflow.find("\n      - name:", start + len(marker))
        return workflow[start:] if end < 0 else workflow[start:end]

    def sequence_mapping_keys(self, text: str, *, indent: int) -> tuple[str, ...]:
        keys: list[str] = []
        pattern = re.compile(rf"^-\s+(?P<key>{YAML_KEY})\s*:")
        for raw_line in text.splitlines():
            active = self.uncomment_yaml_line(raw_line)
            if not active.strip():
                continue
            leading = len(active) - len(active.lstrip(" "))
            if leading != indent or not active[indent:].startswith("-"):
                continue
            content = active[indent:]
            match = pattern.match(content)
            self.assertIsNotNone(
                match,
                f"unsupported sequence mapping syntax at indentation {indent}: "
                f"{content}",
            )
            key = match.group("key")
            if key[:1] in {"'", '"'}:
                key = key[1:-1]
            keys.append(key)
        return tuple(keys)

    def assert_source_consumer_contract(self, adapter: str) -> None:
        jobs = self.mapping_block(adapter, "jobs", indent=0)
        consumer_job = self.mapping_block(jobs, "consumer-tests", indent=2)
        job_keys = self.mapping_keys(consumer_job, indent=4)
        step_keys = self.sequence_mapping_keys(
            consumer_job,
            indent=6,
        ) + self.mapping_keys(consumer_job, indent=8)

        self.assertEqual(
            (
                "timeout-minutes",
                "name",
                "runs-on",
                "outputs",
                "permissions",
                "steps",
            ),
            job_keys,
        )
        self.assertNotIn("continue-on-error", job_keys)
        self.assertNotIn("continue-on-error", step_keys)
        self.assertEqual(
            {"contents": "read"},
            self.permission_map(consumer_job, indent=4),
        )
        self.assertIn("fetch-depth: 0", consumer_job)
        self.assertRegex(
            consumer_job,
            r"(?s)Check out the tagged consumer source.*?path: consumer",
        )
        self.assertRegex(
            consumer_job,
            r"(?s)Check out release-policy at the calling pin.*?"
            r"repository: ryanduguid/release-policy.*?"
            r"ref: \$\{\{ job\.workflow_sha \}\}.*?path: policy",
        )
        self.assertRegex(consumer_job, r"\^\[0-9a-f\]\{40\}\$")
        self.assertIn(
            'test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"',
            consumer_job,
        )
        consumer_checkout = consumer_job.index("Check out the tagged consumer source")
        policy_checkout = consumer_job.index(
            "Check out release-policy at the calling pin"
        )
        python_setup = consumer_job.index("Set up Python")
        pin_check = consumer_job.index("Require an immutable policy pin")
        test_command = "python -B -m unittest discover -s tests -v"
        self.assertLess(consumer_checkout, policy_checkout)
        self.assertLess(policy_checkout, python_setup)
        self.assertLess(python_setup, pin_check)
        self.assertIn('python-version: "3.12"', consumer_job)
        self.assertIn("working-directory: consumer", consumer_job)
        self.assertEqual(1, consumer_job.count(test_command))

    def assert_workflow_call_input_contract(self, workflow: str) -> None:
        trigger = self.mapping_block(workflow, "on", indent=0)
        workflow_call = self.mapping_block(trigger, "workflow_call", indent=2)
        inputs = self.mapping_block(workflow_call, "inputs", indent=4)

        self.assertEqual(
            ("artifact-stem", "source-directory", "tag-prefix", "version-file"),
            self.mapping_keys(inputs, indent=6),
        )

    def assert_source_adapter_release_contract(self, adapter: str) -> None:
        jobs = self.mapping_block(adapter, "jobs", indent=0)
        self.assertEqual(
            self.mapping_keys(jobs, indent=2),
            ("consumer-tests", "release"),
        )
        release_job = self.mapping_block(jobs, "release", indent=2)
        self.assertEqual(
            self.mapping_keys(release_job, indent=4),
            ("needs", "permissions", "uses", "with"),
        )
        self.assertEqual(
            self.mapping_value(release_job, "needs", indent=4),
            "consumer-tests",
        )
        self.assertEqual(
            self.permission_map(release_job, indent=4),
            {
                "attestations": "write",
                "contents": "write",
                "id-token": "write",
            },
        )
        self.assertEqual(
            self.mapping_value(release_job, "uses", indent=4),
            "./.github/workflows/publish-archives.yml",
        )
        inputs = self.mapping_block(release_job, "with", indent=4)
        self.assertEqual(
            self.mapping_keys(inputs, indent=6),
            ("artifact-stem", "source-directory", "tag-prefix", "version-file"),
        )
        self.assertEqual(
            self.mapping_value(inputs, "artifact-stem", indent=6),
            "${{ inputs.artifact-stem }}",
        )
        self.assertEqual(
            self.mapping_value(inputs, "source-directory", indent=6),
            "${{ needs.consumer-tests.outputs.source-directory }}",
        )
        self.assertEqual(
            self.mapping_value(inputs, "tag-prefix", indent=6),
            "${{ needs.consumer-tests.outputs.tag-prefix }}",
        )
        self.assertEqual(
            self.mapping_value(inputs, "version-file", indent=6),
            "${{ inputs.version-file }}",
        )

    def assert_publication_core_permission_contract(self, core: str) -> None:
        jobs = self.mapping_block(core, "jobs", indent=0)
        self.assertEqual(self.mapping_keys(jobs, indent=2), ("publish",))
        publish_job = self.mapping_block(jobs, "publish", indent=2)
        self.assertEqual(
            self.mapping_keys(publish_job, indent=4),
            (
                "timeout-minutes",
                "name",
                "runs-on",
                "permissions",
                "concurrency",
                "steps",
            ),
        )
        self.assertEqual(
            self.permission_map(publish_job, indent=4),
            {
                "attestations": "write",
                "contents": "write",
                "id-token": "write",
            },
        )

    def test_source_adapter_preserves_its_closed_input_and_fixed_test_contract(self) -> None:
        adapter = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
            encoding="utf-8"
        )

        self.assert_workflow_call_input_contract(adapter)
        self.assert_source_consumer_contract(adapter)

    def test_component_inputs_are_validated_before_archive_test_or_publication_use(
        self,
    ) -> None:
        adapter = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
            encoding="utf-8"
        )
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        jobs = self.mapping_block(adapter, "jobs", indent=0)
        consumer = self.mapping_block(jobs, "consumer-tests", indent=2)

        self.assertLess(
            consumer.index("Validate the component release inputs"),
            consumer.index("steps.inputs.outputs.source-path"),
        )
        self.assertIn(
            "working-directory: ${{ steps.inputs.outputs.source-path }}",
            consumer,
        )
        self.assertIn(
            "source-directory: ${{ needs.consumer-tests.outputs.source-directory }}",
            adapter,
        )
        self.assertLess(
            core.index("Validate the component release inputs"),
            core.index("steps.inputs.outputs.source-path"),
        )
        self.assertIn(
            '--source-directory "${{ steps.inputs.outputs.source-directory }}"',
            core,
        )
        self.assertIn(
            "name: ${{ steps.release.outputs.artifact-tag }}-",
            core,
        )
        self.assertNotIn(
            "name: ${{ steps.release.outputs.tag }}-",
            core,
        )

    def test_nested_publication_scopes_every_local_asset_to_the_component(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        build = self.named_step(core, "Build deterministic source archives")
        sbom = self.named_step(core, "Generate an SPDX SBOM for the ZIP archive")
        inventory = self.named_step(core, "Write and verify the exact release asset inventory")
        upload = self.named_step(core, "Preserve the exact candidate assets")
        provenance = self.named_step(core, "Attest release asset provenance")
        sbom_attestation = self.named_step(
            core, "Attest the SPDX SBOM for both source archives"
        )
        verify = self.named_step(
            core, "Verify provenance and SBOM attestations before publication"
        )
        recheck = self.named_step(core, "Re-check the remote tag, main and release absence")
        publish = self.named_step(core, "Create, inspect and publish the immutable release")

        output_match = re.search(r'--output-base "([^"]+)"', build)
        self.assertIsNotNone(output_match)
        output_template = output_match.group(1)  # type: ignore[union-attr]
        self.assertEqual(
            "${{ steps.inputs.outputs.source-path }}/dist/$stem-$version",
            output_template,
        )
        action_base = (
            "${{ steps.inputs.outputs.source-root }}/dist/"
            "${{ steps.release.outputs.stem }}-${{ steps.release.outputs.version }}"
        )
        self.assertIn(f"file: {action_base}.zip", sbom)
        self.assertIn(f"output-file: {action_base}.spdx.json", sbom)
        self.assertIn(
            "working-directory: ${{ steps.inputs.outputs.source-path }}", inventory
        )
        for block in (upload, provenance):
            for suffix in (".zip", ".tar.gz", ".spdx.json"):
                self.assertIn(f"{action_base}{suffix}", block)
            self.assertIn(
                "${{ steps.inputs.outputs.source-root }}/dist/SHA256SUMS", block
            )
        self.assertIn(f"{action_base}.zip", sbom_attestation)
        self.assertIn(f"{action_base}.tar.gz", sbom_attestation)
        self.assertIn(f"sbom-path: {action_base}.spdx.json", sbom_attestation)
        self.assertIn(
            "working-directory: ${{ steps.inputs.outputs.source-path }}", verify
        )
        self.assertIn("working-directory: consumer", recheck)
        self.assertIn("working-directory: consumer", publish)
        self.assertIn(
            "SOURCE_PATH: ${{ steps.inputs.outputs.source-path }}", publish
        )
        publication_script = (
            ROOT / "scripts" / "publish_archives.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('dist="$source_path/dist"', publication_script)
        self.assertIn(
            '--notes-file "$source_path/RELEASE_NOTES.md"', publication_script
        )
        self.assertNotIn("consumer/dist/", core)

        with TemporaryDirectory() as temporary:
            repository = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", "-b", "main"], cwd=repository, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Policy Test"],
                cwd=repository,
                check=True,
            )
            component = repository / "packages" / "example-toolkit"
            component.mkdir(parents=True)
            (repository / "root.txt").write_text("root\n", encoding="utf-8")
            (component / "VERSION").write_text("1.2.3\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "fixture"],
                cwd=repository,
                check=True,
            )
            resolved_output = output_template.replace(
                "${{ steps.inputs.outputs.source-path }}", str(component.resolve())
            ).replace("$stem", "example-toolkit").replace("$version", "1.2.3")
            output_base = Path(resolved_output)
            if not output_base.is_absolute():
                output_base = repository / output_base

            outputs = release_archives.build_release_archives(
                commit="HEAD",
                prefix="example-toolkit-1.2.3/",
                source_directory="packages/example-toolkit",
                output_base=output_base,
                cwd=repository,
            )

            self.assertEqual(component / "dist" / "example-toolkit-1.2.3.zip", outputs[0])
            self.assertEqual(
                component / "dist" / "example-toolkit-1.2.3.tar.gz", outputs[1]
            )
            self.assertFalse((repository / "dist").exists())

    def test_publication_core_delegates_the_release_state_machine_to_policy_code(
        self,
    ) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        publish = self.named_step(core, "Create, inspect and publish the immutable release")
        script = ROOT / "scripts" / "publish_archives.sh"

        self.assertTrue(script.is_file(), "the trusted publication script is missing")
        script_text = script.read_text(encoding="utf-8")
        self.assertIn("working-directory: consumer", publish)
        self.assertIn(
            '"$GITHUB_WORKSPACE/policy/scripts/publish_archives.sh"', publish
        )
        self.assertNotIn("cleanup_current_draft()", publish)
        self.assertIn("cleanup_current_draft()", script_text)
        self.assertNotIn("${{", script_text)
        self.assertIn('--notes-file "$source_path/RELEASE_NOTES.md"', script_text)

    def test_source_consumer_contract_rejects_privilege_and_failure_masking(
        self,
    ) -> None:
        adapter = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
            encoding="utf-8"
        )
        quoted_permission = adapter.replace(
            "    permissions:\n      contents: read\n    steps:\n",
            '    permissions:\n      contents: read\n      "id-token": write\n'
            "    steps:\n",
            1,
        )
        job_failure_mask = adapter.replace(
            "    permissions:\n      contents: read\n",
            "    continue-on-error: true\n"
            "    permissions:\n      contents: read\n",
            1,
        )
        step_failure_mask = adapter.replace(
            "        working-directory: ${{ steps.inputs.outputs.source-path }}\n"
            "        run: python -B -m unittest discover -s tests -v\n",
            "        working-directory: ${{ steps.inputs.outputs.source-path }}\n"
            '        "continue-on-error": true\n'
            "        run: python -B -m unittest discover -s tests -v\n",
            1,
        )
        commented_failure_mask = adapter.replace(
            "    permissions:\n      contents: read\n",
            "    # continue-on-error: true\n"
            "    permissions:\n      contents: read\n",
            1,
        ).replace(
            "        working-directory: ${{ steps.inputs.outputs.source-path }}\n"
            "        run: python -B -m unittest discover -s tests -v\n",
            "        working-directory: ${{ steps.inputs.outputs.source-path }}\n"
            '        # "continue-on-error": true\n'
            "        run: python -B -m unittest discover -s tests -v\n",
            1,
        )

        # Catches consumer code gaining an extra write-capable token permission.
        with self.subTest(mutation="quoted permission"), self.assertRaises(
            AssertionError
        ):
            self.assert_source_consumer_contract(quoted_permission)
        # Catches a consumer job that converts any fixed-step failure to success.
        with self.subTest(mutation="job failure mask"), self.assertRaises(
            AssertionError
        ):
            self.assert_source_consumer_contract(job_failure_mask)
        # Catches the fixed test step converting a failed suite to success.
        with self.subTest(mutation="step failure mask"), self.assertRaises(
            AssertionError
        ):
            self.assert_source_consumer_contract(step_failure_mask)
        # Comments do not change the active job or step contract.
        self.assert_source_consumer_contract(commented_failure_mask)

    def test_source_adapter_delegates_release_without_an_execution_surface(self) -> None:
        adapter = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
            encoding="utf-8"
        )

        self.assert_source_adapter_release_contract(adapter)

    def test_source_adapter_contract_rejects_dependency_and_shape_bypasses(self) -> None:
        adapter = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
            encoding="utf-8"
        )
        dependency_bypass = adapter.replace(
            "  release:\n    needs: consumer-tests\n",
            "  bypass:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: true\n\n"
            "  release:\n"
            "    needs: bypass\n"
            "    # needs: consumer-tests\n",
            1,
        )
        flow_job = adapter[: adapter.index("  release:")] + (
            "  release: {needs: consumer-tests, permissions: {attestations: write, "
            "contents: write, id-token: write}, uses: "
            "./.github/workflows/publish-archives.yml, with: {artifact-stem: "
            '"${{ inputs.artifact-stem }}", version-file: '
            '"${{ inputs.version-file }}"}}\n'
        )
        anchored_job = adapter.replace(
            "  consumer-tests:\n",
            "  consumer-tests: &consumer_tests\n",
            1,
        )
        aliased_job = anchored_job[: anchored_job.index("  release:")] + (
            "  release: *consumer_tests\n"
        )
        quoted_permission = adapter.replace(
            "      id-token: write\n    uses: ",
            '      id-token: write\n      "packages": write\n    uses: ',
            1,
        )

        # Catches a release job that bypasses consumer-tests while retaining the
        # old dependency only in a comment.
        with self.subTest(mutation="dependency bypass"), self.assertRaises(
            AssertionError
        ):
            self.assert_source_adapter_release_contract(dependency_bypass)
        # Catches a contract-owned called job rewritten as an inline flow map.
        with self.subTest(mutation="flow-map job"), self.assertRaises(AssertionError):
            self.assert_source_adapter_release_contract(flow_job)
        # Catches a contract-owned called job supplied through an alias.
        with self.subTest(mutation="aliased job"), self.assertRaises(AssertionError):
            self.assert_source_adapter_release_contract(aliased_job)
        # Catches an extra write permission hidden behind a quoted YAML key.
        with self.subTest(mutation="quoted permission"), self.assertRaises(
            AssertionError
        ):
            self.assert_source_adapter_release_contract(quoted_permission)

    def test_publication_core_has_the_narrow_privileged_contract(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        jobs = self.mapping_block(core, "jobs", indent=0)
        publish_job = self.mapping_block(jobs, "publish", indent=2)

        self.assert_workflow_call_input_contract(core)
        self.assert_publication_core_permission_contract(core)
        self.assertIn("group: release-${{ github.repository }}-${{ github.ref }}", publish_job)
        self.assertIn("cancel-in-progress: false", publish_job)

    def test_source_and_core_interfaces_reject_quoted_flow_and_alias_inputs(
        self,
    ) -> None:
        workflows = tuple(
            (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            for name in ("release-archive.yml", "publish-archives.yml")
        )
        block = (
            "    inputs:\n"
            "      artifact-stem:\n"
            "        description: Lower-case hyphenated stem used for all release assets.\n"
            "        required: true\n"
            "        type: string\n"
            "      source-directory:\n"
            "        description: Tracked component directory containing release sources.\n"
            "        required: false\n"
            "        type: string\n"
            "        default: .\n"
            "      tag-prefix:\n"
            "        description: Optional lower-case hyphenated namespace for the release tag.\n"
            "        required: false\n"
            "        type: string\n"
            '        default: ""\n'
            "      version-file:\n"
            "        description: Safe relative path containing one canonical "
            "MAJOR.MINOR.PATCH line.\n"
            "        required: false\n"
            "        type: string\n"
            "        default: VERSION\n"
        )
        quoted_extra = (
            block
            + '      "command":\n'
            "        description: Unsupported custom command.\n"
            "        required: false\n"
            "        type: string\n"
        )
        flow_inputs = (
            "    inputs: {artifact-stem: {description: Artifact stem, required: true, "
            "type: string}, source-directory: {description: Source directory, required: "
            "false, type: string, default: .}, tag-prefix: {description: Tag prefix, "
            'required: false, type: string, default: ""}, version-file: '
            "{description: Version file, required: false, "
            "type: string, default: VERSION}}\n"
        )
        anchored_inputs = block.replace(
            "    inputs:\n",
            "    inputs: &archive_inputs\n",
            1,
        )
        aliased_triggers = (
            "  workflow_dispatch:\n"
            + anchored_inputs
            + "  workflow_call:\n"
            + "    inputs: *archive_inputs\n"
        )

        for workflow in workflows:
            mutations = {
                "quoted extra": workflow.replace(block, quoted_extra, 1),
                "flow map": workflow.replace(block, flow_inputs, 1),
                "alias": workflow.replace(
                    "  workflow_call:\n" + block,
                    aliased_triggers,
                    1,
                ),
            }
            for mutation_name, mutation in mutations.items():
                self.assertNotEqual(workflow, mutation)
                # Catches a source or core reusable interface that accepts an
                # unsupported extra input or hides its input map syntax.
                with self.subTest(
                    workflow=workflow.splitlines()[0],
                    mutation=mutation_name,
                ), self.assertRaises(AssertionError):
                    self.assert_workflow_call_input_contract(mutation)

    def test_direct_test_entry_point_loads(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_publication_core_permission_contract_rejects_quoted_flow_and_alias(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        quoted_permission = core.replace(
            "      id-token: write\n    concurrency:",
            '      id-token: write\n      "packages": write\n    concurrency:',
            1,
        )
        flow_permission = core.replace(
            "    permissions:\n"
            "      attestations: write\n"
            "      contents: write\n"
            "      id-token: write\n",
            "    permissions: {attestations: write, contents: write, "
            "id-token: write}\n",
            1,
        )
        aliased_permission = core.replace(
            "permissions:\n  contents: read\n",
            "permissions: &privileged\n"
            "  attestations: write\n"
            "  contents: write\n"
            "  id-token: write\n",
            1,
        ).replace(
            "    permissions:\n"
            "      attestations: write\n"
            "      contents: write\n"
            "      id-token: write\n",
            "    permissions: *privileged\n",
            1,
        )

        # Catches an extra privileged permission omitted by the former
        # unquoted-key-only regular expression.
        with self.assertRaises(AssertionError):
            self.assert_publication_core_permission_contract(quoted_permission)
        # Catches a privileged permission block supplied as an inline flow map.
        with self.assertRaises(AssertionError):
            self.assert_publication_core_permission_contract(flow_permission)
        # Catches a privileged permission block supplied through an alias.
        with self.assertRaises(AssertionError):
            self.assert_publication_core_permission_contract(aliased_permission)

    def test_publication_core_uses_sibling_checkouts_and_only_policy_programs(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        publication_script = (
            ROOT / "scripts" / "publish_archives.sh"
        ).read_text(encoding="utf-8")
        policy_code = core + publication_script

        self.assertRegex(
            core,
            r"(?s)Check out the tagged consumer source.*?path: consumer",
        )
        self.assertRegex(
            core,
            r"(?s)Check out release-policy at the calling pin.*?"
            r"repository: ryanduguid/release-policy.*?"
            r"ref: \$\{\{ job\.workflow_sha \}\}.*?path: policy",
        )
        self.assertRegex(core, r"\^\[0-9a-f\]\{40\}\$")
        self.assertIn('test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"', core)
        self.assertIn('$GITHUB_WORKSPACE/policy/scripts/gates.sh', core)
        self.assertIn('$GITHUB_WORKSPACE/policy/scripts/build_release_archives.py', core)
        self.assertIn('$GITHUB_WORKSPACE/policy/scripts/publish_archives.sh', core)
        self.assertIn(
            '$GITHUB_WORKSPACE/policy/scripts/find_created_draft_release.py',
            publication_script,
        )
        self.assertNotIn("../policy/scripts/", policy_code)
        for forbidden in (
            "pip install",
            "unittest discover",
            "scripts/validate_validation.py",
            "tests/verify_skills_cli.py",
            "python scripts/",
            "python tests/",
            "./scripts/",
            "./tests/",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, policy_code)

    def test_publication_core_preserves_candidates_assets_and_signer_identity(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        publication_script = (
            ROOT / "scripts" / "publish_archives.sh"
        ).read_text(encoding="utf-8")
        upload_start = core.index("uses: actions/upload-artifact@")
        upload_end = core.index("      - name:", upload_start)
        upload = core[upload_start:upload_end]
        action_asset_paths = re.findall(
            r"^            (\$\{\{ steps\.inputs\.outputs\.source-root \}\}/dist/[^\r\n]+)$",
            core,
            re.MULTILINE,
        )

        self.assertIn(
            "name: ${{ steps.release.outputs.artifact-tag }}-${{ github.run_id }}-"
            "${{ github.run_attempt }}-candidate-assets",
            upload,
        )
        self.assertIn("overwrite: false", upload)
        self.assertEqual(4, len(set(action_asset_paths)))
        self.assertNotIn("consumer/dist/", core)
        self.assertIn(
            "working-directory: ${{ steps.inputs.outputs.source-path }}",
            core,
        )
        self.assertIn(
            '--notes-file "$source_path/RELEASE_NOTES.md"', publication_script
        )
        self.assertIn(
            'signer="ryanduguid/release-policy/.github/workflows/publish-archives.yml"',
            core,
        )
        self.assertNotIn("github.repository_owner", core)

    def test_publication_core_preserves_exact_asset_and_publication_gates(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        publication_script = (
            ROOT / "scripts" / "publish_archives.sh"
        ).read_text(encoding="utf-8")
        policy_code = core + publication_script

        for required in (
            "SHA256SUMS",
            ".spdx.json",
            ".tar.gz",
            ".zip",
            "actions/attest@",
            "actions/upload-artifact@",
            "find_created_draft_release.py",
            "gh attestation verify",
            "gh release verify-asset",
            "gate_main_matches",
            "gate_no_existing_release",
        ):
            with self.subTest(required=required):
                self.assertIn(required, policy_code)

        self.assertIn("--draft", publication_script)
        self.assertIn("-F draft=false", publication_script)
        final_recheck = 'final_tag_commit="$(git ls-remote'
        publish = "gh api --method PATCH"
        self.assertIn(final_recheck, publication_script)
        self.assertIn(
            'gate_main_matches "$expected_commit" "$GITHUB_REPOSITORY"',
            publication_script,
        )
        self.assertGreater(
            publication_script.index(final_recheck),
            publication_script.index("/tmp/draft-digests"),
        )
        self.assertGreater(
            publication_script.index(publish),
            publication_script.index(final_recheck),
        )
        publish_block = publication_script[
            publication_script.index(publish) : publication_script.index(publish) + 300
        ]
        self.assertIn("repos/$GITHUB_REPOSITORY/releases/$release_id", publish_block)

    def test_neither_archive_workflow_uses_forbidden_handoff_or_upload_paths(self) -> None:
        workflows = tuple(
            (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            for name in ("release-archive.yml", "publish-archives.yml")
        )

        for workflow in workflows:
            for forbidden in (
                ".release-policy-verified",
                "secrets: inherit",
                "gh release upload",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, workflow)


class CreatedDraftLookupTests(unittest.TestCase):
    def test_selects_only_the_exact_created_untagged_draft(self) -> None:
        created_url = "https://github.com/example/tool/releases/tag/untagged-created"
        releases = [
            {
                "id": 41,
                "html_url": "https://github.com/example/tool/releases/tag/v9.9.9",
                "draft": True,
                "prerelease": False,
            },
            {
                "id": 42,
                "html_url": created_url,
                "draft": True,
                "prerelease": False,
            },
        ]

        self.assertEqual(
            "42",
            draft_release.select_created_draft_release_id(releases, created_url),
        )

    def test_retries_visibility_transient_api_and_partial_json_failures(self) -> None:
        created_url = "https://api.github.com/repos/example/tool/releases/42"
        created = {
            "id": 42,
            "url": created_url,
            "draft": True,
            "prerelease": False,
        }
        failures: tuple[BaseException | list[dict[str, object]], ...] = (
            draft_release.ReleaseNotVisibleError("not visible yet"),
            subprocess.CalledProcessError(
                1,
                ["gh", "api"],
                stderr="HTTP 503: Service Unavailable",
            ),
            json.JSONDecodeError("partial", '{"id":', 6),
            [created],
        )
        results = iter(failures)
        delays: list[float] = []

        def list_releases() -> list[dict[str, object]]:
            result = next(results)
            if isinstance(result, BaseException):
                raise result
            return result

        self.assertEqual(
            "42",
            draft_release.find_created_draft_release_id(
                list_releases,
                created_url,
                attempts=4,
                delay_seconds=2,
                sleep=delays.append,
            ),
        )
        self.assertEqual([2, 2, 2], delays)

    def test_tracks_the_exact_id_until_the_requested_tag_is_visible(self) -> None:
        created_url = "https://github.com/example/tool/releases/tag/untagged-created"
        listings = iter(
            (
                [
                    {
                        "id": 42,
                        "html_url": created_url,
                        "tag_name": "untagged-created",
                        "draft": True,
                        "prerelease": False,
                    }
                ],
                [
                    {
                        "id": 42,
                        "html_url": "https://github.com/example/tool/releases/tag/v1.2.3",
                        "tag_name": "v1.2.3",
                        "draft": True,
                        "prerelease": False,
                    },
                    {
                        "id": 43,
                        "html_url": created_url,
                        "tag_name": "v9.9.9",
                        "draft": True,
                        "prerelease": False,
                    },
                ],
            )
        )
        delays: list[float] = []

        self.assertEqual(
            "42",
            draft_release.find_created_draft_release_id(
                lambda: next(listings),
                created_url,
                expected_tag="v1.2.3",
                attempts=2,
                delay_seconds=2,
                sleep=delays.append,
            ),
        )
        self.assertEqual([2], delays)

    def test_fails_immediately_for_credentials_or_an_ineligible_match(self) -> None:
        created_url = "https://api.github.com/repos/example/tool/releases/42"
        credentials = mock.Mock(
            side_effect=subprocess.CalledProcessError(
                1,
                ["gh", "api"],
                stderr="HTTP 401: Bad credentials",
            )
        )
        with self.assertRaises(subprocess.CalledProcessError):
            draft_release.find_created_draft_release_id(
                credentials,
                created_url,
                attempts=5,
                delay_seconds=0,
            )
        self.assertEqual(1, credentials.call_count)

        ineligible = mock.Mock(
            return_value=[
                {
                    "id": 42,
                    "url": created_url,
                    "draft": False,
                    "prerelease": False,
                }
            ]
        )
        with self.assertRaisesRegex(
            draft_release.ReleaseLookupError,
            "not an eligible draft",
        ):
            draft_release.find_created_draft_release_id(
                ineligible,
                created_url,
                attempts=5,
                delay_seconds=0,
            )
        self.assertEqual(1, ineligible.call_count)


if __name__ == "__main__":
    unittest.main()
