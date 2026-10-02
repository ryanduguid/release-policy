from __future__ import annotations

import hashlib
import importlib
import io
import os
import runpy
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
installer = importlib.import_module("pr_review_install")
review = importlib.import_module("pr_review")
RAW = b"public archive fixture"


def locked_packages():
    return [{"name": name, "version": version, "source": {"registry": "https://pypi.org/simple"},
             "sdist": {"url": f"https://files.pythonhosted.org/packages/{name}-{version}.tar.gz",
                       "hash": "sha256:" + hashlib.sha256(RAW).hexdigest(), "size": len(RAW)}}
            for name, version in installer.EXCEPTIONS.items()]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Path(self.directory.name)
        (self.engine / "uv.lock").write_text("fixture")
        self.packages = locked_packages()
        self.git = self.enterContext(mock.patch.object(installer.subprocess, "run", side_effect=[
            types.SimpleNamespace(stdout=review.PR_AGENT_SHA), types.SimpleNamespace(stdout="")]))
        self.toml = self.enterContext(mock.patch.object(installer.tomllib, "loads",
                                                       return_value={"package": self.packages}))
        self.opener = self.enterContext(mock.patch.object(installer, "build_opener"))
        self.opener.return_value.open.side_effect = lambda *args, **kwargs: io.BytesIO(RAW)
        self.enterContext(mock.patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "", "NOUS_API_KEY": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""}))

    def test_prepare_uses_pinned_local_archives_without_credentials(self):
        self.assertEqual(installer.main(["prepare", str(self.engine)]), 0)
        requirements = (self.engine / "pr-review-source-exceptions.txt").read_text()
        for name, version in installer.EXCEPTIONS.items():
            path = self.engine / f"{name}-{version}.tar.gz"
            self.assertEqual(path.read_bytes(), RAW)
            self.assertIn(path.resolve().as_posix(), requirements)
            self.assertIn("--hash=sha256:" + hashlib.sha256(RAW).hexdigest(), requirements)
        for call in self.opener.return_value.open.call_args_list:
            self.assertEqual(call.args[0].headers, {})
        self.assertIsInstance(self.opener.call_args.args[0], review.NoRedirect)

    def test_dirty_or_wrong_engine_stops_before_reading_archives(self):
        for revision, dirty in (("wrong", ""), (review.PR_AGENT_SHA, " M uv.lock")):
            self.git.side_effect = [types.SimpleNamespace(stdout=revision), types.SimpleNamespace(stdout=dirty)]
            with self.assertRaisesRegex(review.ReviewError, "engine_pin_mismatch"):
                installer.prepare_sources(self.engine)
        self.opener.assert_not_called()

    def test_unexpected_or_duplicate_source_package_is_not_added(self):
        for packages in (self.packages[:1], self.packages + [self.packages[0]],
                         [self.packages[0], {**self.packages[1], "version": "wrong"}]):
            self.git.side_effect = [types.SimpleNamespace(stdout=review.PR_AGENT_SHA), types.SimpleNamespace(stdout="")]
            self.toml.return_value = {"package": packages}
            with self.assertRaisesRegex(review.ReviewError, "unexpected_source_dependency"):
                installer.prepare_sources(self.engine)
        self.opener.assert_not_called()

    def test_archive_origin_filename_size_and_checksum_fail_closed(self):
        variants = [{"url": "http://files.pythonhosted.org/giteapy-1.0.8.tar.gz"},
                    {"url": "https://files.pythonhosted.org/packages/other.tar.gz"},
                    {"size": True}, {"size": 0}, {"size": 1_000_001},
                    {"size": len(RAW) + 1}, {"hash": "sha256:" + "0" * 64}]
        for changes in variants:
            with self.subTest(changes=changes):
                self.git.side_effect = [types.SimpleNamespace(stdout=review.PR_AGENT_SHA), types.SimpleNamespace(stdout="")]
                self.toml.return_value = {"package": [{**self.packages[0], "sdist": {**self.packages[0]["sdist"], **changes}}, self.packages[1]]}
                with self.assertRaises(review.ReviewError):
                    installer.prepare_sources(self.engine)
        self.git.side_effect = [types.SimpleNamespace(stdout=review.PR_AGENT_SHA), types.SimpleNamespace(stdout="")]
        self.toml.return_value = {"package": [{**self.packages[0], "source": {"registry": "https://other.invalid"}}, self.packages[1]]}
        with self.assertRaisesRegex(review.ReviewError, "untrusted_archive_source"):
            installer.prepare_sources(self.engine)
        self.assertFalse((self.engine / "pr-review-source-exceptions.txt").exists())

    def metadata_fixture(self, requirements=None, distributions=None):
        if requirements is None:
            requirements = "giteapy==1.0.8\nhtml2text==2024.2.26\nabsent==1 ; python_version < '1'\n"
        (self.engine / "pr-review-runtime-requirements.txt").write_text(requirements)
        items = [types.SimpleNamespace(metadata={"Name": name}, version=version)
                 for name, version in installer.EXCEPTIONS.items()]
        self.enterContext(mock.patch.object(installer.importlib.metadata, "distributions", return_value=items if distributions is None else distributions))
        return self.enterContext(mock.patch.object(installer.importlib.metadata, "distribution",
                                                   side_effect=lambda name: next(item for item in items if item.metadata["Name"] == name)))

    def test_complete_runtime_set_and_source_metadata_match(self):
        self.metadata_fixture()
        self.assertEqual(installer.main(["verify", str(self.engine)]), 0)

    def test_unlocked_duplicate_or_extra_runtime_distribution_fails(self):
        for text, code in (("giteapy>=1\n", "unlocked_runtime_requirement"),
                           ("giteapy\n", "unlocked_runtime_requirement"),
                           ("giteapy==1\ngiteapy==1\n", "duplicate_runtime_requirement")):
            (self.engine / "pr-review-runtime-requirements.txt").write_text(text)
            with self.assertRaisesRegex(review.ReviewError, code):
                installer.verify_packages(self.engine)
        self.metadata_fixture(distributions=[])
        with self.assertRaisesRegex(review.ReviewError, "runtime_lock_metadata_mismatch"):
            installer.verify_packages(self.engine)

    def test_source_distribution_metadata_is_rechecked(self):
        distribution = self.metadata_fixture()
        distribution.side_effect = None
        for name, version in (("other", "1.0.8"), ("giteapy", "wrong")):
            distribution.return_value = types.SimpleNamespace(metadata={"Name": name}, version=version)
            with self.assertRaisesRegex(review.ReviewError, "source_package_metadata_mismatch"):
                installer.verify_packages(self.engine)

    def test_credential_guard_and_safe_cli_errors(self):
        with redirect_stderr(io.StringIO()) as errors:
            for name in ("OPENROUTER_API_KEY", "NOUS_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"):
                with mock.patch.dict(os.environ, {name: "fixture"}):
                    self.assertEqual(installer.main(["prepare", str(self.engine)]), 1)
            self.opener.assert_not_called()
            self.git.assert_not_called()
            self.git.side_effect = OSError("private error")
            self.assertEqual(installer.main(["prepare", str(self.engine)]), 1)
            self.assertNotIn("private error", errors.getvalue())
            self.assertIn("engine_install_failed", errors.getvalue())
            with mock.patch.object(sys, "argv", [str(ROOT / "scripts/pr_review_install.py"), "prepare", str(self.engine)]), self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(ROOT / "scripts/pr_review_install.py"), run_name="__main__")
            self.assertEqual(caught.exception.code, 1)
