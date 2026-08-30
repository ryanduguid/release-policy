from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

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
