"""Refusal paths of python_release that the round-trip tests do not reach.

Each case is one way a consumer checkout or a candidate directory can be wrong;
the module must refuse it with its own message, never pass it through.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts import python_release

import test_python_release as base


class MetadataRefusalTests(base.RepositoryFixture):
    def project(self, text: str = '[project]\nname = "demo-package"\nversion = "1.2.3"\n') -> None:
        self.write("pyproject.toml", text)

    def derive(self, parser: str = "pyproject", version_file: str = "pyproject.toml"):
        return python_release.derive_metadata(
            self.root, version_parser=parser, version_file=version_file
        )

    def test_rejects_a_backslash_version_file_path(self) -> None:
        self.project()
        self.commit()
        with self.assertRaisesRegex(ValueError, "safe relative POSIX path"):
            self.derive(version_file="pkg\\version.py")

    def test_rejects_a_version_file_that_is_a_directory(self) -> None:
        self.project()
        self.write("pkg/__init__.py", "")
        self.commit()
        with self.assertRaisesRegex(ValueError, "regular file inside the checkout"):
            self.derive(version_file="pkg")

    def test_rejects_a_version_file_over_the_metadata_limit(self) -> None:
        self.project()
        self.commit()
        with mock.patch.object(python_release, "_MAX_METADATA_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "exceeds the 1 MiB metadata limit"):
                self.derive()

    def test_rejects_a_bom_or_non_utf8_pyproject(self) -> None:
        for raw, message in (
            (b"\xef\xbb\xbf[project]\nname = 'x'\n", "must not contain a UTF-8 BOM"),
            (b"[project]\nname = '\xff'\n", "must be UTF-8"),
        ):
            with self.subTest(message=message):
                (self.root / "pyproject.toml").write_bytes(raw)
                self.commit()
                with self.assertRaisesRegex(ValueError, message):
                    self.derive()

    def test_rejects_a_missing_or_blank_project_name(self) -> None:
        for text, message in (
            ("[tool.other]\nx = 1\n", r"must declare \[project\]\.name"),
            ('[project]\nname = "  "\nversion = "1.2.3"\n', "must be a non-empty string"),
        ):
            with self.subTest(message=message):
                self.project(text)
                self.commit()
                with self.assertRaisesRegex(ValueError, message):
                    self.derive()

    def test_rejects_a_missing_or_non_string_pyproject_version(self) -> None:
        for text, message in (
            ('[project]\nname = "demo"\n', r"requires \[project\]\.version"),
            ('[project]\nname = "demo"\nversion = 1\n', "must be a string"),
        ):
            with self.subTest(message=message):
                self.project(text)
                self.commit()
                with self.assertRaisesRegex(ValueError, message):
                    self.derive()

    def test_python_literal_reads_one_direct_string_assignment(self) -> None:
        self.project()
        self.write("pkg/version.py", 'other = 1\n__version__ = "1.2.3"\n')
        self.commit()
        metadata = self.derive(parser="python-literal", version_file="pkg/version.py")
        self.assertEqual(metadata.version, "1.2.3")

    def test_python_literal_refusals(self) -> None:
        for source, message in (
            ('__version__ = other = "1.2.3"\n', "one direct literal string assignment"),
            ("__version__: str\n", "one direct literal string assignment"),
            ("__version__ = 1\n", "one direct literal string assignment"),
            ("def broken(:\n", "valid Python source"),
        ):
            with self.subTest(source=source):
                self.project()
                self.write("pkg/version.py", source)
                self.commit()
                with self.assertRaisesRegex(ValueError, message):
                    self.derive(parser="python-literal", version_file="pkg/version.py")

    def test_rejects_a_name_that_makes_no_safe_wheel_stem(self) -> None:
        self.project('[project]\nname = "demo package"\nversion = "1.2.3"\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, "safe wheel stem"):
            self.derive()

    def test_metadata_command_prints_lines_or_json(self) -> None:
        self.project()
        self.commit()
        for output_format, expected in (
            ("lines", "demo_package\n1.2.3\n"),
            ("json", '{"name": "demo-package", "stem": "demo_package", "version": "1.2.3"}\n'),
        ):
            with self.subTest(output_format=output_format):
                printed = io.StringIO()
                with redirect_stdout(printed):
                    code = python_release.main([
                        "metadata", "--root", str(self.root), "--version-parser", "pyproject",
                        "--version-file", "pyproject.toml", "--format", output_format,
                    ])
                self.assertEqual(code, 0)
                self.assertEqual(printed.getvalue(), expected)


class CandidateRefusalTests(unittest.TestCase):
    STEM = base.CandidateInventoryTests.STEM
    VERSION = base.CandidateInventoryTests.VERSION
    TAG = base.CandidateInventoryTests.TAG
    REPOSITORY = base.CandidateInventoryTests.REPOSITORY
    COMMIT = base.CandidateInventoryTests.COMMIT
    POLICY_SHA = base.CandidateInventoryTests.POLICY_SHA
    RUN_ID = base.CandidateInventoryTests.RUN_ID
    RUN_ATTEMPT = base.CandidateInventoryTests.RUN_ATTEMPT

    setUp = base.CandidateInventoryTests.setUp
    tearDown = base.CandidateInventoryTests.tearDown
    create = base.CandidateInventoryTests.create
    verify = base.CandidateInventoryTests.verify

    def candidate_arguments(self, **changes) -> dict:
        arguments = dict(
            stem=self.STEM, version=self.VERSION, tag=self.TAG, repository=self.REPOSITORY,
            commit=self.COMMIT, policy_sha=self.POLICY_SHA, run_id=self.RUN_ID,
            run_attempt=self.RUN_ATTEMPT,
        )
        arguments.update(changes)
        return arguments

    def test_rejects_an_unsafe_workflow_context(self) -> None:
        for change, message in (
            ({"stem": "Bad Stem"}, "stem is not safe"),
            ({"repository": "no-owner"}, "repository must be owner/name"),
            ({"commit": "abc"}, "source commit must be a full lower-case SHA"),
            ({"policy_sha": "A" * 40}, "policy SHA must be a full lower-case SHA"),
            ({"run_id": 0}, "run id and attempt must be positive"),
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, message):
                    python_release.write_candidate_inventory(
                        self.dist, **self.candidate_arguments(**change)
                    )

    def test_rejects_an_empty_asset_and_a_stray_file(self) -> None:
        wheel = self.dist / f"{self.STEM}-{self.VERSION}-py3-none-any.whl"
        wheel.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "invalid size"):
            self.create()
        wheel.write_bytes(b"wheel\n")
        (self.dist / "notes.txt").write_text("stray\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "candidate payload inventory mismatch"):
            self.create()

    def test_regular_asset_rejects_a_directory(self) -> None:
        directory = self.dist / "folder"
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, "must be a regular file: folder"):
            python_release._regular_asset(directory)

    def manifest(self) -> dict:
        return json.loads((self.dist / "release-manifest.json").read_text(encoding="utf-8"))

    def write_manifest(self, document: dict, *, canonical: bool = True) -> None:
        text = json.dumps(document, indent=2 if canonical else None, sort_keys=True) + "\n"
        (self.dist / "release-manifest.json").write_bytes(text.encode("utf-8"))

    def test_rejects_malformed_manifest_documents(self) -> None:
        def drop_package(document: dict) -> None:
            del document["package"]

        def schema_two(document: dict) -> None:
            document["schema"] = 2

        def other_signer(document: dict) -> None:
            document["policy"]["workflow"] = ".github/workflows/other.yml"

        def assets_mapping(document: dict) -> None:
            document["assets"] = {}

        def numeric_name(document: dict) -> None:
            document["assets"][0]["name"] = 1

        def text_run_id(document: dict) -> None:
            document["run"]["id"] = "not a number"

        for change, message in (
            (drop_package, "root has an invalid shape"),
            (schema_two, "schema is unsupported"),
            (other_signer, "policy signer is not release-python.yml"),
            (assets_mapping, "assets must be a list"),
            (numeric_name, "asset text fields are invalid"),
            (text_run_id, "scalar fields are invalid"),
        ):
            with self.subTest(change=change.__name__):
                self.tearDown()
                self.setUp()
                self.create()
                document = self.manifest()
                change(document)
                self.write_manifest(document)
                with self.assertRaisesRegex(ValueError, message):
                    self.verify()

    def test_rejects_an_oversized_unparsable_or_non_canonical_manifest(self) -> None:
        self.create()
        with mock.patch.object(python_release, "_MAX_METADATA_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "exceeds the 1 MiB limit"):
                self.verify()
        document = self.manifest()
        self.write_manifest(document, canonical=False)
        with self.assertRaisesRegex(ValueError, "not canonical JSON"):
            self.verify()
        (self.dist / "release-manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            self.verify()

    def test_rejects_asset_records_that_disagree_with_the_files(self) -> None:
        def renamed(assets):
            return (replace(assets[0], name="other.whl"), *assets[1:])

        def duplicated(assets):
            return (*assets, assets[0])

        def wrong_type(assets):
            return (replace(assets[0], media_type="text/plain"), *assets[1:])

        def wrong_size(assets):
            return (replace(assets[0], size=assets[0].size + 1), *assets[1:])

        for change, message in (
            (renamed, "payload inventory is invalid"),
            (duplicated, "contains duplicate assets"),
            (wrong_type, "media type mismatch"),
            (wrong_size, "size mismatch"),
        ):
            with self.subTest(change=change.__name__):
                self.tearDown()
                self.setUp()
                created = self.create()
                tampered = replace(created, assets=change(created.assets))
                (self.dist / "release-manifest.json").write_bytes(
                    python_release._manifest_bytes(tampered)
                )
                with self.assertRaisesRegex(ValueError, message):
                    self.verify()

    def test_rejects_an_altered_checksum_inventory(self) -> None:
        self.create()
        checksums = self.dist / "SHA256SUMS"
        checksums.write_text(checksums.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum inventory or digest mismatch"):
            self.verify()

    def test_create_and_verify_commands_round_trip(self) -> None:
        arguments = [
            "--directory", str(self.dist), "--stem", self.STEM, "--version", self.VERSION,
            "--tag", self.TAG, "--repository", self.REPOSITORY, "--commit", self.COMMIT,
            "--policy-sha", self.POLICY_SHA, "--run-id", str(self.RUN_ID),
            "--run-attempt", str(self.RUN_ATTEMPT),
        ]
        self.assertEqual(python_release.main(["create-candidate", *arguments]), 0)
        self.assertEqual(python_release.main(["verify-candidate", *arguments]), 0)
        self.assertTrue(Path(self.dist / "SHA256SUMS").is_file())


if __name__ == "__main__":
    unittest.main()
