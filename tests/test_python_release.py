from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import python_release


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="release-policy-python-")
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Release Policy Test"],
            cwd=self.root,
            check=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def commit(self) -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "fixture"],
            cwd=self.root,
            check=True,
        )


class MetadataTests(RepositoryFixture):
    def test_reads_static_pyproject_without_executing_consumer_code(self) -> None:
        self.write(
            "pyproject.toml",
            """[project]
name = "demo-package"
version = "1.2.3"
""",
        )
        self.write("consumer.py", "raise RuntimeError('must never execute')\n")
        self.commit()

        metadata = python_release.derive_metadata(
            self.root,
            version_parser="pyproject",
            version_file="pyproject.toml",
        )

        self.assertEqual(metadata.name, "demo-package")
        self.assertEqual(metadata.stem, "demo_package")
        self.assertEqual(metadata.version, "1.2.3")

    def test_normalises_dotted_and_mixed_case_names_to_the_built_wheel_stem(self) -> None:
        self.write(
            "pyproject.toml",
            """[project]
name = "Demo.Pkg"
version = "1.2.3"
""",
        )
        self.commit()

        metadata = python_release.derive_metadata(
            self.root,
            version_parser="pyproject",
            version_file="pyproject.toml",
        )

        self.assertEqual(metadata.name, "Demo.Pkg")
        self.assertEqual(metadata.stem, "demo_pkg")
        self.assertEqual(metadata.version, "1.2.3")
        # The stem must name the file the build backend actually writes, which
        # is what the SBOM, candidate inventory and release steps look for.
        self.assertEqual(
            "demo_pkg-1.2.3-py3-none-any.whl",
            python_release._payload_contract(metadata.stem, metadata.version)[0][0],
        )

    def test_reads_one_literal_version_assignment_without_importing_module(self) -> None:
        self.write(
            "pyproject.toml",
            """[project]
name = "dynamic-demo"
dynamic = ["version"]
""",
        )
        self.write(
            "src/dynamic_demo/version.py",
            "raise RuntimeError('must never execute')\n__version__: str = '2.3.4'\n",
        )
        self.commit()

        metadata = python_release.derive_metadata(
            self.root,
            version_parser="python-literal",
            version_file="src/dynamic_demo/version.py",
        )

        self.assertEqual(metadata.name, "dynamic-demo")
        self.assertEqual(metadata.stem, "dynamic_demo")
        self.assertEqual(metadata.version, "2.3.4")

    def test_rejects_nonliteral_duplicate_and_noncanonical_versions(self) -> None:
        self.write(
            "pyproject.toml",
            """[project]
name = "dynamic-demo"
dynamic = ["version"]
""",
        )
        version_path = self.write("version.py", "__version__ = make_version()\n")
        self.commit()

        with self.assertRaisesRegex(ValueError, "literal string"):
            python_release.derive_metadata(
                self.root,
                version_parser="python-literal",
                version_file="version.py",
            )

        version_path.write_text(
            "__version__ = '1.2.3'\n__version__ = '1.2.3'\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            python_release.derive_metadata(
                self.root,
                version_parser="python-literal",
                version_file="version.py",
            )

        version_path.write_text("__version__ = '01.2.3'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "canonical"):
            python_release.derive_metadata(
                self.root,
                version_parser="python-literal",
                version_file="version.py",
            )

    def test_rejects_unknown_parser_untracked_path_traversal_and_shell_payload(self) -> None:
        self.write(
            "pyproject.toml",
            """[project]
name = "demo-package"
version = "1.2.3"
""",
        )
        self.commit()
        self.write("untracked.py", "__version__ = '1.2.3'\n")
        sentinel = self.root / "executed"

        cases = (
            ("shell", "pyproject.toml"),
            ("python-literal", "../version.py"),
            ("python-literal", "untracked.py"),
            ("python-literal", "version.py; touch executed"),
        )
        for parser, version_file in cases:
            with self.subTest(parser=parser, version_file=version_file):
                with self.assertRaises(ValueError):
                    python_release.derive_metadata(
                        self.root,
                        version_parser=parser,
                        version_file=version_file,
                    )
        self.assertFalse(sentinel.exists())

    def test_rejects_symlinked_version_file(self) -> None:
        self.write(
            "pyproject.toml",
            """[project]
name = "dynamic-demo"
dynamic = ["version"]
""",
        )
        target = self.write("real-version.py", "__version__ = '1.2.3'\n")
        link = self.root / "version.py"
        try:
            os.symlink(target, link)
        except OSError as error:
            self.skipTest(f"symlinks are unavailable: {error}")
        self.commit()

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            python_release.derive_metadata(
                self.root,
                version_parser="python-literal",
                version_file="version.py",
            )


class ReleaseInputTests(RepositoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write("packages/example/source.txt", "tracked\n")
        self.write("tracked-file.txt", "tracked\n")
        self.commit()

    def invoke(self, *arguments: str) -> str:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(0, python_release.main(list(arguments)))
        return output.getvalue()

    def test_validates_component_and_derives_all_tag_forms(self) -> None:
        self.assertEqual(
            python_release.ReleaseInputs("packages/example", "example"),
            python_release.validate_release_inputs(
                self.root,
                source_directory="packages/example",
                tag_prefix="example",
            ),
        )
        self.assertEqual(".", python_release.validate_source_directory(self.root, "."))
        self.assertEqual(
            python_release.ReleaseTag(
                version="1.2.3",
                version_tag="v1.2.3",
                full_tag="example/v1.2.3",
                artifact_tag="example-v1.2.3",
                tag_prefix="example",
            ),
            python_release.parse_release_tag("example/v1.2.3", "example"),
        )
        self.assertEqual(
            "v1.2.3",
            python_release.derive_release_tag("1.2.3", "").full_tag,
        )

    def test_cli_emits_json_and_line_protocols(self) -> None:
        inputs_json = json.loads(
            self.invoke(
                "validate-inputs",
                "--root",
                str(self.root),
                "--source-directory",
                "packages/example",
                "--tag-prefix",
                "example",
            )
        )
        self.assertEqual(
            {"source_directory": "packages/example", "tag_prefix": "example"},
            inputs_json,
        )
        self.assertEqual(
            "packages/example\nexample\n",
            self.invoke(
                "validate-inputs",
                "--root",
                str(self.root),
                "--source-directory",
                "packages/example",
                "--tag-prefix",
                "example",
                "--format",
                "lines",
            ),
        )

        tag_json = json.loads(
            self.invoke("tag", "--tag", "example/v1.2.3", "--tag-prefix", "example")
        )
        self.assertEqual("example-v1.2.3", tag_json["artifact_tag"])
        self.assertEqual(
            "1.2.3\nv1.2.3\nexample/v1.2.3\nexample-v1.2.3\n",
            self.invoke(
                "tag",
                "--tag",
                "example/v1.2.3",
                "--tag-prefix",
                "example",
                "--format",
                "lines",
            ),
        )

    def test_rejects_unsafe_component_paths_and_tag_forms(self) -> None:
        (self.root / "packages" / "untracked").mkdir()
        unsafe = (
            "",
            "packages\\example",
            "C:/packages/example",
            "packages/control\x1f",
            "/packages/example",
            "packages//example",
            "packages/./example",
            "packages/../example",
            "packages/[example]",
            "packages/missing",
            "packages/untracked",
            "tracked-file.txt",
        )
        for supplied in unsafe:
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                python_release.validate_source_directory(self.root, supplied)

        with mock.patch.object(Path, "is_symlink", return_value=True), self.assertRaisesRegex(
            ValueError, "symbolic link"
        ):
            python_release.validate_source_directory(self.root, "packages/example")
        with mock.patch.object(
            python_release,
            "_git_index_entries",
            return_value=(("160000", b"packages"),),
        ), self.assertRaisesRegex(ValueError, "gitlink"):
            python_release.validate_source_directory(self.root, "packages/example")

        failed = subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=b"failure")
        with mock.patch.object(python_release.subprocess, "run", return_value=failed), self.assertRaisesRegex(
            ValueError, "Git index"
        ):
            python_release._git_index_entries(self.root, "packages/example")
        malformed = subprocess.CompletedProcess(
            ["git"], 0, stdout=b"malformed\0", stderr=b""
        )
        with mock.patch.object(
            python_release.subprocess, "run", return_value=malformed
        ), self.assertRaisesRegex(ValueError, "malformed"):
            python_release._git_index_entries(self.root, "packages/example")

        for prefix in ("Example", "example--tool", "example/tool"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                python_release.validate_release_inputs(
                    self.root,
                    source_directory=".",
                    tag_prefix=prefix,
                )
        with self.assertRaisesRegex(ValueError, "canonical"):
            python_release.derive_release_tag("01.2.3", "")
        with self.assertRaisesRegex(ValueError, "does not match"):
            python_release.parse_release_tag("other/v1.2.3", "example")
        with self.assertRaisesRegex(ValueError, "canonical"):
            python_release.parse_release_tag("example/v1.2", "example")

        mismatched = python_release.ReleaseTag(
            version="1.2.3",
            version_tag="v1.2.3",
            full_tag="different/v1.2.3",
            artifact_tag="different-v1.2.3",
            tag_prefix="different",
        )
        with mock.patch.object(
            python_release, "derive_release_tag", return_value=mismatched
        ), self.assertRaisesRegex(ValueError, "not canonical"):
            python_release.parse_release_tag("example/v1.2.3", "example")


class CandidateInventoryTests(unittest.TestCase):
    STEM = "demo_pkg"
    VERSION = "1.2.3"
    TAG = "v1.2.3"
    REPOSITORY = "ryanduguid/demo-package"
    COMMIT = "1" * 40
    POLICY_SHA = "2" * 40
    RUN_ID = 12345
    RUN_ATTEMPT = 2

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="release-candidate-")
        self.dist = Path(self.temporary.name)
        for name, payload in (
            (f"{self.STEM}-{self.VERSION}-py3-none-any.whl", b"wheel\n"),
            (f"{self.STEM}-{self.VERSION}.tar.gz", b"sdist\n"),
            (f"{self.STEM}-{self.VERSION}.spdx.json", b'{"spdxVersion":"SPDX-2.3"}\n'),
        ):
            (self.dist / name).write_bytes(payload)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self) -> python_release.CandidateInventory:
        return python_release.write_candidate_inventory(
            self.dist,
            stem=self.STEM,
            version=self.VERSION,
            tag=self.TAG,
            repository=self.REPOSITORY,
            commit=self.COMMIT,
            policy_sha=self.POLICY_SHA,
            run_id=self.RUN_ID,
            run_attempt=self.RUN_ATTEMPT,
        )

    def verify(self) -> python_release.CandidateInventory:
        return python_release.verify_candidate_inventory(
            self.dist,
            expected_stem=self.STEM,
            expected_version=self.VERSION,
            expected_tag=self.TAG,
            expected_repository=self.REPOSITORY,
            expected_commit=self.COMMIT,
            expected_policy_sha=self.POLICY_SHA,
            expected_run_id=self.RUN_ID,
            expected_run_attempt=self.RUN_ATTEMPT,
        )

    def test_round_trips_exact_candidate_and_checksum_inventory(self) -> None:
        created = self.create()
        verified = self.verify()

        self.assertEqual(created, verified)
        self.assertEqual(
            sorted(path.name for path in self.dist.iterdir()),
            sorted(
                (
                    f"{self.STEM}-{self.VERSION}-py3-none-any.whl",
                    f"{self.STEM}-{self.VERSION}.tar.gz",
                    f"{self.STEM}-{self.VERSION}.spdx.json",
                    "release-manifest.json",
                    "SHA256SUMS",
                )
            ),
        )
        manifest = json.loads((self.dist / "release-manifest.json").read_text())
        self.assertEqual(manifest["source"]["repository"], self.REPOSITORY)
        self.assertEqual(manifest["source"]["ref"], f"refs/tags/{self.TAG}")
        self.assertEqual(manifest["policy"]["sha"], self.POLICY_SHA)
        self.assertEqual(manifest["run"], {"attempt": self.RUN_ATTEMPT, "id": self.RUN_ID})
        self.assertEqual(len(manifest["assets"]), 3)
        self.assertEqual(len((self.dist / "SHA256SUMS").read_text().splitlines()), 4)

    def test_round_trips_namespaced_source_tag_without_slash_bearing_assets(self) -> None:
        tag = "payday-super-checker/v1.2.3"
        created = python_release.write_candidate_inventory(
            self.dist,
            stem=self.STEM,
            version=self.VERSION,
            tag=tag,
            repository=self.REPOSITORY,
            commit=self.COMMIT,
            policy_sha=self.POLICY_SHA,
            run_id=self.RUN_ID,
            run_attempt=self.RUN_ATTEMPT,
        )
        verified = python_release.verify_candidate_inventory(
            self.dist,
            expected_stem=self.STEM,
            expected_version=self.VERSION,
            expected_tag=tag,
            expected_repository=self.REPOSITORY,
            expected_commit=self.COMMIT,
            expected_policy_sha=self.POLICY_SHA,
            expected_run_id=self.RUN_ID,
            expected_run_attempt=self.RUN_ATTEMPT,
        )

        self.assertEqual(created, verified)
        manifest = json.loads((self.dist / "release-manifest.json").read_text())
        self.assertEqual("refs/tags/payday-super-checker/v1.2.3", manifest["source"]["ref"])
        self.assertTrue(all("/" not in path.name for path in self.dist.iterdir()))

    def test_rejects_noncanonical_context_and_non_tag_manifest_ref(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical version"):
            python_release._validate_context(
                stem=self.STEM,
                version="01.2.3",
                tag="v01.2.3",
                repository=self.REPOSITORY,
                commit=self.COMMIT,
                policy_sha=self.POLICY_SHA,
                run_id=self.RUN_ID,
                run_attempt=self.RUN_ATTEMPT,
            )

        mismatched = python_release.ReleaseTag(
            version=self.VERSION,
            version_tag=f"v{self.VERSION}",
            full_tag="different/v1.2.3",
            artifact_tag="different-v1.2.3",
            tag_prefix="different",
        )
        with mock.patch.object(
            python_release, "derive_release_tag", return_value=mismatched
        ), self.assertRaisesRegex(ValueError, "do not match"):
            python_release._validate_context(
                stem=self.STEM,
                version=self.VERSION,
                tag=self.TAG,
                repository=self.REPOSITORY,
                commit=self.COMMIT,
                policy_sha=self.POLICY_SHA,
                run_id=self.RUN_ID,
                run_attempt=self.RUN_ATTEMPT,
            )

        self.create()
        manifest = json.loads((self.dist / "release-manifest.json").read_text())
        manifest["source"]["ref"] = "refs/heads/main"
        with self.assertRaisesRegex(ValueError, "source ref"):
            python_release._inventory_from_manifest(manifest)

    def test_rejects_tamper_extra_file_and_context_mismatch(self) -> None:
        self.create()
        wheel = self.dist / f"{self.STEM}-{self.VERSION}-py3-none-any.whl"
        wheel.write_bytes(b"bogus\n")
        with self.assertRaisesRegex(ValueError, "digest"):
            self.verify()

        wheel.write_bytes(b"wheel\n")
        self.create = lambda: None  # type: ignore[method-assign]
        (self.dist / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.verify()

        (self.dist / "extra.txt").unlink()
        with self.assertRaisesRegex(ValueError, "source commit"):
            python_release.verify_candidate_inventory(
                self.dist,
                expected_stem=self.STEM,
                expected_version=self.VERSION,
                expected_tag=self.TAG,
                expected_repository=self.REPOSITORY,
                expected_commit="3" * 40,
                expected_policy_sha=self.POLICY_SHA,
                expected_run_id=self.RUN_ID,
                expected_run_attempt=self.RUN_ATTEMPT,
            )

    def test_refuses_to_overwrite_control_files_or_accept_symlinks(self) -> None:
        (self.dist / "release-manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.create()

        (self.dist / "release-manifest.json").unlink()
        wheel = self.dist / f"{self.STEM}-{self.VERSION}-py3-none-any.whl"
        target = self.dist / "real.whl"
        target.write_bytes(wheel.read_bytes())
        wheel.unlink()
        try:
            os.symlink(target, wheel)
        except OSError as error:
            self.skipTest(f"symlinks are unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.create()

    def test_rejects_a_symlinked_candidate_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-candidate-link-") as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            for source in self.dist.iterdir():
                (real / source.name).write_bytes(source.read_bytes())
            link = root / "dist"
            try:
                os.symlink(real, link, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlinks are unavailable: {error}")

            with self.assertRaisesRegex(ValueError, "regular directory"):
                python_release.write_candidate_inventory(
                    link,
                    stem=self.STEM,
                    version=self.VERSION,
                    tag=self.TAG,
                    repository=self.REPOSITORY,
                    commit=self.COMMIT,
                    policy_sha=self.POLICY_SHA,
                    run_id=self.RUN_ID,
                    run_attempt=self.RUN_ATTEMPT,
                )


if __name__ == "__main__":
    unittest.main()
