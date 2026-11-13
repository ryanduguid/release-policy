# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""The PyPI read-back against recorded, fabricated PyPI answers.

Each refusal is one way what PyPI serves can differ from what was built; the
check must fail closed on it. No test reaches the network.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from scripts import verify_pypi

COMMIT = "1" * 40
REPOSITORY = "example-owner/demo-pkg"
WORKFLOW = "release.yml"
WHEEL = "demo_pkg-1.2.3-py3-none-any.whl"
SDIST = "demo_pkg-1.2.3.tar.gz"
JSON_URL = "https://pypi.org/pypi/demo-pkg/1.2.3/json"
DIGEST_OID = bytes.fromhex("2b0601040183bf30010d")
OTHER_OID = bytes.fromhex("2b0601040183bf30010c")


def der(tag: int, contents: bytes) -> bytes:
    length = len(contents)
    if length < 0x80:
        return bytes([tag, length]) + contents
    size = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(size)]) + size + contents


def certificate(commit: str | bytes | None = COMMIT, text_tag: int = 0x0C) -> bytes:
    """A fabricated certificate shaped like Fulcio's: only the parts the check reads."""
    uri = b"https://github.com/example-owner/demo-pkg/.github/workflows/release.yml@refs/tags/v1.2.3"
    extensions = [der(0x30, der(0x06, OTHER_OID) + der(0x04, der(0x0C, uri)))]
    if commit is not None:
        value = der(text_tag, commit.encode() if isinstance(commit, str) else commit)
        extensions.append(der(0x30, der(0x06, DIGEST_OID) + der(0x04, value)))
    tbs = der(0x30, der(0xA0, der(0x02, b"\x02")) + der(0x02, b"\x01") + der(0x30, b"")
              + der(0xA3, der(0x30, b"".join(extensions))))
    return der(0x30, tbs + der(0x30, b"") + der(0x03, b"\x00"))


def attestation(cert: bytes) -> dict:
    return {"version": 1, "envelope": {"statement": "e30=", "signature": "c2ln"},
            "verification_material": {"certificate": base64.b64encode(cert).decode(),
                                      "transparency_entries": []}}


def provenance(cert: bytes | None = None, **publisher: object) -> bytes:
    named = {"kind": "GitHub", "repository": REPOSITORY, "workflow": WORKFLOW,
             "environment": "pypi", "claims": None, **publisher}
    bundle = {"publisher": named, "attestations": [attestation(cert or certificate())]}
    return json.dumps({"version": 1, "attestation_bundles": [bundle]}).encode()


def release(files: dict[str, bytes]) -> bytes:
    urls = [{"filename": name, "digests": {"md5": "0" * 32,
                                           "sha256": hashlib.sha256(data).hexdigest()}}
            for name, data in files.items()]
    return json.dumps({"info": {"version": "1.2.3"}, "urls": urls}).encode()


def integrity_url(name: str) -> str:
    return f"https://pypi.org/integrity/demo-pkg/1.2.3/{name}/provenance"


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.built = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
        self.paths = []
        for name, data in self.built.items():
            (self.directory / name).write_bytes(data)
            self.paths.append(self.directory / name)
        self.answers: dict[str, object] = {
            JSON_URL: release(self.built),
            integrity_url(WHEEL): provenance(),
            integrity_url(SDIST): provenance(),
        }
        self.requests: list[tuple[str, str]] = []
        self.verified: list[tuple[Path, bytes, str]] = []
        self.sleeps: list[float] = []

    def get(self, url: str, accept: str) -> bytes | None:
        self.requests.append((url, accept))
        answer = self.answers[url]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    def verify(self, file: Path, provenance_file: Path, repository: str) -> None:
        self.verified.append((file, provenance_file.read_bytes(), repository))

    def run_check(self, project: str = "demo_pkg", version: str = "1.2.3",
                  **overrides: object) -> str:
        arguments: dict[str, object] = {
            "repository": REPOSITORY, "commit": COMMIT, "workflow": WORKFLOW,
            "get": self.get, "verify": self.verify, "attempts": 3, "delay": 7.0,
            "sleep": self.sleeps.append,
        }
        arguments.update(overrides)
        files = arguments.pop("files", self.paths)
        output = io.StringIO()
        with redirect_stdout(output):
            verify_pypi.verify_publication(project, version, files, **arguments)  # type: ignore[arg-type]
        return output.getvalue()

    def refuses(self, pattern: str, **overrides: object) -> None:
        with self.assertRaisesRegex(ValueError, pattern):
            self.run_check(**overrides)


class ReadBackTests(Fixture):
    def test_passes_when_pypi_serves_the_built_files_with_their_provenance(self) -> None:
        output = self.run_check()
        self.assertIn(f"PyPI serves demo-pkg 1.2.3 as built, with provenance from {REPOSITORY}",
                      output)
        self.assertEqual(
            self.requests,
            [(JSON_URL, "application/json"),
             (integrity_url(WHEEL), "application/vnd.pypi.integrity.v1+json"),
             (integrity_url(SDIST), "application/vnd.pypi.integrity.v1+json")],
        )
        self.assertEqual(
            self.verified,
            [(self.directory / WHEEL, provenance(), REPOSITORY),
             (self.directory / SDIST, provenance(), REPOSITORY)],
        )
        self.assertEqual(self.sleeps, [])

    def test_refuses_a_built_file_pypi_does_not_serve(self) -> None:
        self.answers[JSON_URL] = release({WHEEL: self.built[WHEEL]})
        self.refuses(r"PyPI does not serve these files for demo-pkg 1\.2\.3: \['demo_pkg-1\.2\.3\.tar\.gz'\]")
        self.assertEqual(self.sleeps, [7.0, 7.0])
        self.assertEqual(self.verified, [])

    def test_refuses_a_served_file_that_was_not_built(self) -> None:
        self.answers[JSON_URL] = release({**self.built, "demo_pkg-1.2.3-py2-none-any.whl": b"x"})
        self.refuses(r"files for demo-pkg 1\.2\.3 that were not built: \['demo_pkg-1\.2\.3-py2")
        self.assertEqual(self.sleeps, [])

    def test_refuses_a_served_file_whose_digest_differs(self) -> None:
        self.answers[JSON_URL] = release({WHEEL: self.built[WHEEL], SDIST: b"other sdist"})
        self.refuses(f"PyPI serves {SDIST} with SHA-256 {hashlib.sha256(b'other sdist').hexdigest()}")
        self.assertEqual(self.verified, [])

    def test_refuses_a_release_pypi_does_not_serve(self) -> None:
        self.answers[JSON_URL] = None
        self.refuses(r"PyPI does not serve demo-pkg 1\.2\.3$")
        self.assertEqual(self.sleeps, [7.0, 7.0])

    def test_reads_again_until_pypi_serves_every_built_file(self) -> None:
        self.answers[JSON_URL] = [None, release({WHEEL: self.built[WHEEL]}), release(self.built)]
        self.run_check()
        self.assertEqual(self.sleeps, [7.0, 7.0])
        self.assertEqual(len(self.verified), 2)

    def test_refuses_a_file_pypi_serves_no_provenance_for(self) -> None:
        self.answers[integrity_url(WHEEL)] = None
        self.refuses(f"PyPI serves no provenance for {WHEEL}")

    def test_refuses_provenance_from_another_publisher(self) -> None:
        for publisher in ({"repository": "someone/else"}, {"workflow": "other.yml"},
                          {"kind": "GitLab"}):
            with self.subTest(publisher=publisher):
                self.answers[integrity_url(SDIST)] = provenance(**publisher)
                self.refuses(f"provenance for {SDIST} names publisher .*, expected")
        bundle = {"publisher": None, "attestations": []}
        self.answers[integrity_url(SDIST)] = json.dumps(
            {"version": 1, "attestation_bundles": [bundle]}).encode()
        self.refuses(f"provenance for {SDIST} names publisher None")

    def test_refuses_provenance_signed_at_another_commit(self) -> None:
        self.answers[integrity_url(SDIST)] = provenance(certificate("2" * 40))
        self.refuses(f"provenance for {SDIST} was signed at commit {'2' * 40}, expected {COMMIT}")

    def test_refuses_a_certificate_that_names_no_source_commit(self) -> None:
        self.answers[integrity_url(SDIST)] = provenance(certificate(None))
        self.refuses(f"provenance for {SDIST} was signed at commit None")

    def test_refuses_unverifiable_provenance(self) -> None:
        def unverifiable(file: Path, provenance_file: Path, repository: str) -> None:
            raise ValueError(f"provenance for {file.name} does not verify: bad signature")

        self.refuses(f"provenance for {WHEEL} does not verify: bad signature", verify=unverifiable)

    def test_fails_closed_on_a_network_failure(self) -> None:
        failure = ValueError("https://pypi.org could not be read: timed out")
        for url in (JSON_URL, integrity_url(WHEEL)):
            with self.subTest(url=url):
                saved, self.answers[url] = self.answers[url], failure
                self.refuses("could not be read: timed out")
                self.answers[url] = saved

    def test_refuses_malformed_provenance(self) -> None:
        bundle = {"publisher": {"kind": "GitHub", "repository": REPOSITORY, "workflow": WORKFLOW}}

        def document(*bundles: object, version: int = 1) -> bytes:
            return json.dumps({"version": version, "attestation_bundles": list(bundles)}).encode()

        cases = [
            ("is not JSON", b"{"),
            ("is not a version 1 provenance object", b"[]"),
            ("is not a version 1 provenance object", document(version=2)),
            ("holds no attestation bundles", document()),
            ("has a bundle with no attestations", document(bundle)),
            ("has a bundle with no attestations", document({**bundle, "attestations": []})),
            ("has no readable certificate", document({**bundle, "attestations": ["x"]})),
        ]
        for value in (None, "not base64!", 7):
            material = {"verification_material": {"certificate": value}}
            cases.append(("has no readable certificate",
                          document({**bundle, "attestations": [material]})))
        for message, raw in cases:
            with self.subTest(message=message, raw=raw):
                self.answers[integrity_url(SDIST)] = raw
                self.refuses(f"provenance for {SDIST} {message}")

    def test_refuses_a_malformed_release_document(self) -> None:
        entry = {"filename": WHEEL, "digests": {"sha256": "a" * 64}}
        cases = [
            ("is not JSON", b"<html>"),
            ("has no file list", b"[]"),
            ("has no file list", json.dumps({"urls": {}}).encode()),
            ("has an unreadable file entry", json.dumps({"urls": ["x"]}).encode()),
            ("has an unreadable file entry", json.dumps(
                {"urls": [{"filename": WHEEL, "digests": {"sha256": "A" * 64}}]}).encode()),
            ("has an unreadable file entry", json.dumps(
                {"urls": [{"filename": 3, "digests": []}]}).encode()),
            (f"lists {WHEEL} twice", json.dumps({"urls": [entry, entry]}).encode()),
        ]
        for message, raw in cases:
            with self.subTest(message=message, raw=raw):
                self.answers[JSON_URL] = raw
                self.refuses(f"PyPI's JSON for demo-pkg 1.2.3 {message}")

    def test_refuses_unsafe_inputs_before_any_request(self) -> None:
        cases = [
            ("is not a valid project name", {"project": "../x"}),
            ("is not a valid version", {"version": "1.2.3/../x"}),
            ("is not an OWNER/REPO repository", {"repository": "owner"}),
            ("is not a full commit SHA", {"commit": "abc"}),
            ("is not a workflow file name", {"workflow": ".github/workflows/release.yml"}),
            ("is not a regular distribution file", {"files": [self.directory / "absent.whl"]}),
            ("is not a regular distribution file", {"files": [self.directory]}),
            ("is named twice", {"files": [self.paths[0], self.paths[0]]}),
            ("no files to compare", {"files": []}),
        ]
        for message, overrides in cases:
            with self.subTest(message=message, overrides=overrides):
                self.refuses(message, **overrides)
        self.assertEqual(self.requests, [])


class CertificateTests(unittest.TestCase):
    def test_reads_the_source_commit_extension(self) -> None:
        self.assertEqual(verify_pypi.certificate_commit(certificate()), COMMIT)
        self.assertIsNone(verify_pypi.certificate_commit(certificate(None)))

    def test_skips_a_certificate_without_extensions(self) -> None:
        tbs = der(0x30, der(0x02, b"\x01"))
        self.assertIsNone(verify_pypi.certificate_commit(der(0x30, tbs)))

    def test_refuses_unreadable_der(self) -> None:
        cases = {
            "truncated": certificate()[:-1],
            "empty": b"",
            "two top-level elements": certificate() + certificate(),
            "header only": b"\x30",
            "indefinite length": b"\x30\x80",
            "length longer than its bytes": b"\x30\x82\x01",
            "empty certificate": der(0x30, b""),
            "not a UTF8String": certificate(text_tag=0x13),
            "not UTF-8": certificate(b"\xff" * 40),
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "signing certificate is not readable"):
                    verify_pypi.certificate_commit(data)


class FetchTests(unittest.TestCase):
    def open(self, body: bytes = b"{}") -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        return response

    def test_returns_the_body_and_sends_only_the_accept_header(self) -> None:
        with mock.patch.object(verify_pypi, "urlopen", return_value=self.open()) as opened:
            self.assertEqual(verify_pypi.fetch("https://pypi.org/a", "application/json"), b"{}")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://pypi.org/a")
        self.assertEqual(request.header_items(), [("Accept", "application/json")])
        self.assertEqual(opened.call_args.kwargs, {"timeout": 60})

    def test_returns_none_for_404_and_fails_closed_otherwise(self) -> None:
        def error(code: int) -> HTTPError:
            return HTTPError("https://pypi.org/a", code, "status", {}, None)  # type: ignore[arg-type]

        with mock.patch.object(verify_pypi, "urlopen", side_effect=error(404)):
            self.assertIsNone(verify_pypi.fetch("https://pypi.org/a", "application/json"))
        for failure, message in ((error(503), "answered HTTP 503"),
                                 (URLError("no route"), "could not be read: .*no route"),
                                 (TimeoutError("timed out"), "could not be read: timed out")):
            with self.subTest(message=message):
                with mock.patch.object(verify_pypi, "urlopen", side_effect=failure):
                    with self.assertRaisesRegex(ValueError, message):
                        verify_pypi.fetch("https://pypi.org/a", "application/json")
        large = self.open(b"x" * (16 * 1024 * 1024 + 1))
        with mock.patch.object(verify_pypi, "urlopen", return_value=large):
            with self.assertRaisesRegex(ValueError, "more than 16 MiB"):
                verify_pypi.fetch("https://pypi.org/a", "application/json")


class VerifyAttestationsTests(unittest.TestCase):
    file = Path("dist") / WHEEL
    provenance = Path("scratch") / f"{WHEEL}.provenance"

    def run_verify(self, **result: object) -> mock.MagicMock:
        completed = subprocess.CompletedProcess([], **{"returncode": 0, "stderr": "", **result})
        with mock.patch.object(verify_pypi.subprocess, "run", return_value=completed) as run:
            verify_pypi.verify_attestations(self.file, self.provenance, REPOSITORY)
        return run

    def test_runs_pypi_attestations_on_the_local_file_and_saved_provenance(self) -> None:
        run = self.run_verify()
        command = run.call_args.args[0]
        self.assertEqual(command[1:], [
            "-m", "pypi_attestations", "verify", "pypi",
            "--repository", f"https://github.com/{REPOSITORY}",
            "--provenance-file", str(self.provenance), str(self.file)])
        self.assertFalse(run.call_args.kwargs["check"])

    def test_fails_closed_when_verification_fails_or_stalls(self) -> None:
        for stderr, reason in (("debug\nVerification failed for x: bad\n", "Verification failed"),
                               ("", "no message")):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, f"{WHEEL} does not verify: {reason}"):
                    self.run_verify(returncode=1, stderr=stderr)
        stalled = subprocess.TimeoutExpired(["python"], 600)
        with mock.patch.object(verify_pypi.subprocess, "run", side_effect=stalled):
            with self.assertRaisesRegex(ValueError, "did not finish verifying"):
                verify_pypi.verify_attestations(self.file, self.provenance, REPOSITORY)


class MainTests(unittest.TestCase):
    arguments = ["--project", "demo_pkg", "--version", "1.2.3", "--repository", REPOSITORY,
                 "--commit", COMMIT, "--workflow", WORKFLOW, "--file", WHEEL, "--file", SDIST]

    def test_passes_the_arguments_through_and_exits_0(self) -> None:
        with mock.patch.object(verify_pypi, "verify_publication") as check:
            self.assertEqual(verify_pypi.main(self.arguments), 0)
        check.assert_called_once_with("demo_pkg", "1.2.3", [Path(WHEEL), Path(SDIST)],
                                      repository=REPOSITORY, commit=COMMIT, workflow=WORKFLOW)

    def test_reports_a_refusal_on_one_line_and_exits_1(self) -> None:
        stderr = io.StringIO()
        refusal = ValueError("PyPI serves no provenance for x")
        with mock.patch.object(verify_pypi, "verify_publication", side_effect=refusal):
            with redirect_stderr(stderr):
                self.assertEqual(verify_pypi.main(self.arguments), 1)
        self.assertEqual(stderr.getvalue(), "verify_pypi: PyPI serves no provenance for x\n")


if __name__ == "__main__":
    unittest.main()
