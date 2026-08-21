from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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


class ReleaseArchiveWorkflowTests(unittest.TestCase):
    def test_workflow_has_a_small_pinned_source_archive_contract(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_call:", workflow)
        self.assertIn("artifact-stem:", workflow)
        self.assertIn("version-file:", workflow)
        self.assertNotIn("test-command:", workflow)
        self.assertNotIn("archive-glob:", workflow)
        self.assertIn("${{ job.workflow_sha }}", workflow)
        self.assertNotIn(r"\${", workflow)
        self.assertIn("scripts/build_release_archives.py", workflow)
        self.assertIn("python -B -m unittest discover -s tests -v", workflow)

    def test_workflow_preserves_exact_asset_and_publication_gates(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-archive.yml").read_text(
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
                self.assertIn(required, workflow)

        self.assertNotIn("gh release upload", workflow)
        self.assertIn("--draft", workflow)
        self.assertIn("-F draft=false", workflow)
        self.assertIn(
            'signer="ryanduguid/release-policy/.github/workflows/release-archive.yml"',
            workflow,
        )
        self.assertNotIn("github.repository_owner", workflow)


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
