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
sys.path.insert(0, str(ROOT / "scripts"))

import build_release_archives as release_archives  # noqa: E402
import find_created_draft_release as draft_release  # noqa: E402
from tests.test_skill_workflows import YamlContractAssertions  # noqa: E402


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
    def test_builder_pins_git_conversion_and_timezone(self) -> None:
        with TemporaryDirectory() as temporary, mock.patch.object(
            release_archives.subprocess,
            "run",
        ) as run:
            outputs = release_archives.build_release_archives(
                commit="deadbeef",
                prefix="example-1.2.3/",
                output_base=Path(temporary) / "dist" / "example-1.2.3",
                cwd=ROOT,
            )

        self.assertEqual(2, run.call_count)
        self.assertEqual(
            ("example-1.2.3.zip", "example-1.2.3.tar.gz"),
            tuple(path.name for path in outputs),
        )
        for call in run.call_args_list:
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
            ("artifact-stem", "version-file"),
        )
        self.assertEqual(
            self.mapping_value(inputs, "artifact-stem", indent=6),
            "${{ inputs.artifact-stem }}",
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

    def test_source_adapter_preserves_its_two_input_and_fixed_test_contract(self) -> None:
        adapter = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
            encoding="utf-8"
        )
        input_block = adapter[adapter.index("    inputs:") : adapter.index("\n\npermissions:")]
        input_names = set(
            re.findall(r"^      ([a-z][a-z0-9-]*):$", input_block, re.MULTILINE)
        )
        consumer_job = adapter[
            adapter.index("  consumer-tests:") : adapter.index("  release:")
        ]

        self.assertEqual({"artifact-stem", "version-file"}, input_names)
        self.assertIn("contents: read", consumer_job)
        self.assertNotIn("contents: write", consumer_job)
        self.assertNotIn("attestations: write", consumer_job)
        self.assertNotIn("id-token: write", consumer_job)
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
        self.assertIn('test "$(git -C policy rev-parse HEAD)" = "$MODULE_SHA"', consumer_job)
        consumer_checkout = consumer_job.index("Check out the tagged consumer source")
        policy_checkout = consumer_job.index("Check out release-policy at the calling pin")
        python_setup = consumer_job.index("Set up Python")
        pin_check = consumer_job.index("Require an immutable policy pin")
        test_command = "python -B -m unittest discover -s tests -v"
        self.assertLess(consumer_checkout, policy_checkout)
        self.assertLess(policy_checkout, python_setup)
        self.assertLess(python_setup, pin_check)
        self.assertIn('python-version: "3.12"', consumer_job)
        self.assertIn("working-directory: consumer", consumer_job)
        self.assertEqual(1, consumer_job.count(test_command))

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
        input_block = core[core.index("    inputs:") : core.index("\n\npermissions:")]
        input_names = set(
            re.findall(r"^      ([a-z][a-z0-9-]*):$", input_block, re.MULTILINE)
        )
        jobs = self.mapping_block(core, "jobs", indent=0)
        publish_job = self.mapping_block(jobs, "publish", indent=2)

        self.assertEqual({"artifact-stem", "version-file"}, input_names)
        self.assert_publication_core_permission_contract(core)
        self.assertIn("group: release-${{ github.repository }}-${{ github.ref }}", publish_job)
        self.assertIn("cancel-in-progress: false", publish_job)

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
        self.assertIn(". ../policy/scripts/gates.sh", core)
        self.assertIn("python ../policy/scripts/build_release_archives.py", core)
        self.assertIn("python ../policy/scripts/find_created_draft_release.py", core)
        self.assertGreaterEqual(core.count("working-directory: consumer"), 6)
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
                self.assertNotIn(forbidden, core)

    def test_publication_core_preserves_candidates_assets_and_signer_identity(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )
        upload_start = core.index("uses: actions/upload-artifact@")
        upload_end = core.index("      - name:", upload_start)
        upload = core[upload_start:upload_end]
        action_asset_paths = re.findall(
            r"^            (consumer/dist/[^\r\n]+)$",
            core,
            re.MULTILINE,
        )

        self.assertIn(
            "name: ${{ steps.release.outputs.tag }}-${{ github.run_id }}-"
            "${{ github.run_attempt }}-candidate-assets",
            upload,
        )
        self.assertIn("overwrite: false", upload)
        self.assertEqual(4, len(set(action_asset_paths)))
        self.assertNotRegex(core, r"^            dist/", msg="action paths must include consumer/")
        self.assertIn("working-directory: consumer", core)
        self.assertIn("--notes-file RELEASE_NOTES.md", core)
        self.assertIn(
            'signer="ryanduguid/release-policy/.github/workflows/publish-archives.yml"',
            core,
        )
        self.assertNotIn("github.repository_owner", core)

    def test_publication_core_preserves_exact_asset_and_publication_gates(self) -> None:
        core = (ROOT / ".github" / "workflows" / "publish-archives.yml").read_text(
            encoding="utf-8"
        )

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
                self.assertIn(required, core)

        self.assertIn("--draft", core)
        self.assertIn("-F draft=false", core)
        final_recheck = 'final_tag_commit="$(git ls-remote'
        publish = "gh api --method PATCH"
        self.assertIn(final_recheck, core)
        self.assertIn('gate_main_matches "$expected_commit" "$GITHUB_REPOSITORY"', core)
        self.assertGreater(core.index(final_recheck), core.index("/tmp/draft-digests"))
        self.assertGreater(core.index(publish), core.index(final_recheck))
        publish_block = core[core.index(publish) : core.index(publish) + 300]
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
