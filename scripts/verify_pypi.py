# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""Read a publication back from PyPI and compare it with the files that were built.

usage: python scripts/verify_pypi.py --project NAME --version VERSION
           --repository OWNER/REPO --commit SHA --workflow FILE --file PATH [--file PATH ...]

Run after the upload, with pypi-attestations installed from
requirements-pypi-verify.txt. It reads PyPI's JSON API for the release and
fails unless PyPI serves exactly the named files with their SHA-256 digests.
For each file it then reads the provenance from PyPI's Integrity API and fails
unless every attestation bundle names the GitHub publisher REPOSITORY and
WORKFLOW, every attestation's signing certificate names COMMIT as its source
commit, and pypi-attestations verifies every attestation against the local
file. A missing file, extra file, digest mismatch, missing or unverifiable
provenance, redirect away from https://pypi.org or failed request fails; only
a release or file PyPI does not serve yet is read again, for up to 5 minutes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Callable, Sequence
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_PYPI = "https://pypi.org"
_INTEGRITY_ACCEPT = "application/vnd.pypi.integrity.v1+json"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_PROJECT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!_-]*\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WORKFLOW = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml\Z")
# Fulcio's Source Repository Digest extension, 1.3.6.1.4.1.57264.1.13: the
# commit the signing workflow ran at, as a DER UTF8String.
_SOURCE_DIGEST_OID = bytes.fromhex("2b0601040183bf30010d")

Fetch = Callable[[str, str], bytes | None]
Verify = Callable[[Path, Path, str], None]


class _PyPIRedirects(HTTPRedirectHandler):
    """Follow a redirect only to another https://pypi.org URL."""

    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> Request | None:
        if urlsplit(newurl)[:2] != ("https", "pypi.org"):
            fp.close()
            raise ValueError(f"{req.full_url} redirected to {newurl}, outside https://pypi.org")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_open = build_opener(_PyPIRedirects()).open


def fetch(url: str, accept: str) -> bytes | None:
    """GET a PyPI URL; None when PyPI answers 404, ValueError on any other failure."""
    try:
        with _open(Request(url, headers={"Accept": accept}), timeout=60) as response:
            body: bytes = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise ValueError(f"{url} answered HTTP {error.code}") from error
    except OSError as error:
        raise ValueError(f"{url} could not be read: {error}") from error
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError(f"{url} answered more than 16 MiB")
    return body


def verify_attestations(file: Path, provenance: Path, repository: str) -> None:
    """Verify every attestation in a provenance file against the local file, or fail."""
    command = [
        sys.executable, "-m", "pypi_attestations", "verify", "pypi",
        "--repository", f"https://github.com/{repository}",
        "--provenance-file", str(provenance), str(file),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=600)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"pypi-attestations did not finish verifying {file.name}") from error
    except OSError as error:
        raise ValueError(f"pypi-attestations could not be run: {error}") from error
    if result.returncode != 0:
        reason = (result.stderr.strip().splitlines() or ["no message"])[-1]
        raise ValueError(f"provenance for {file.name} does not verify: {reason}")


def _element(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read the DER element at offset: its tag, contents and the offset after it."""
    if offset + 2 > len(data):
        raise ValueError("truncated DER element")
    tag, length = data[offset], data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if not 0 < count <= 4 or offset + count > len(data):
            raise ValueError("unsupported DER length")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    if offset + length > len(data):
        raise ValueError("truncated DER element")
    return tag, data[offset : offset + length], offset + length


def _elements(data: bytes) -> list[tuple[int, bytes]]:
    items, offset = [], 0
    while offset < len(data):
        tag, contents, offset = _element(data, offset)
        items.append((tag, contents))
    return items


def certificate_commit(der: bytes) -> str | None:
    """Return the source commit a signing certificate names, or None if it names none."""
    commits: list[str] = []
    try:
        ((_, certificate),) = _elements(der)
        _, tbs = _elements(certificate)[0]
        for tag, block in _elements(tbs):
            if tag != 0xA3:  # [3] EXPLICIT Extensions
                continue
            ((_, extensions),) = _elements(block)
            for _, extension in _elements(extensions):
                parts = _elements(extension)
                if parts[0] == (0x06, _SOURCE_DIGEST_OID):
                    ((text_tag, text),) = _elements(parts[-1][1])
                    if text_tag != 0x0C:
                        raise ValueError("source commit is not a UTF8String")
                    commits.append(text.decode("utf-8"))
    except (ValueError, IndexError) as error:
        raise ValueError(f"signing certificate is not readable: {error}") from error
    # RFC 5280 section 4.2: a certificate must not include an extension twice.
    if len(commits) > 1:
        raise ValueError("signing certificate names its source commit more than once")
    return commits[0] if commits else None


def _served_files(raw: bytes, label: str) -> dict[str, str]:
    """Map each file PyPI serves for the release to its SHA-256 digest."""
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"PyPI's JSON for {label} is not JSON") from error
    urls = document.get("urls") if isinstance(document, dict) else None
    if not isinstance(urls, list):
        raise ValueError(f"PyPI's JSON for {label} has no file list")
    served: dict[str, str] = {}
    for entry in urls:
        name = entry.get("filename") if isinstance(entry, dict) else None
        digests = entry.get("digests") if isinstance(entry, dict) else None
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(name, str) or not isinstance(digest, str) or not _SHA256.match(digest):
            raise ValueError(f"PyPI's JSON for {label} has an unreadable file entry")
        if name in served:
            raise ValueError(f"PyPI's JSON for {label} lists {name} twice")
        served[name] = digest
    return served


def _check_provenance(raw: bytes, name: str, repository: str, commit: str, workflow: str) -> None:
    """Fail unless every bundle names the expected publisher and every certificate the commit."""
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"provenance for {name} is not JSON") from error
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError(f"provenance for {name} is not a version 1 provenance object")
    bundles = document.get("attestation_bundles")
    if not isinstance(bundles, list) or not bundles:
        raise ValueError(f"provenance for {name} holds no attestation bundles")
    expected = {"kind": "GitHub", "repository": repository, "workflow": workflow}
    for bundle in bundles:
        publisher = bundle.get("publisher") if isinstance(bundle, dict) else None
        actual = (
            {key: publisher.get(key) for key in expected} if isinstance(publisher, dict) else None
        )
        if actual != expected:
            raise ValueError(f"provenance for {name} names publisher {actual}, expected {expected}")
        attestations = bundle.get("attestations")
        if not isinstance(attestations, list) or not attestations:
            raise ValueError(f"provenance for {name} has a bundle with no attestations")
        for attestation in attestations:
            try:
                der = base64.b64decode(
                    attestation["verification_material"]["certificate"], validate=True
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"provenance for {name} has no readable certificate") from error
            signed_at = certificate_commit(der)
            if signed_at != commit:
                raise ValueError(
                    f"provenance for {name} was signed at commit {signed_at}, expected {commit}"
                )


def verify_publication(
    project: str,
    version: str,
    files: Sequence[Path],
    *,
    repository: str,
    commit: str,
    workflow: str,
    get: Fetch = fetch,
    verify: Verify = verify_attestations,
    attempts: int = 21,
    delay: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Fail closed unless PyPI serves exactly FILES, each with verified provenance."""
    if not _PROJECT.match(project):
        raise ValueError(f"'{project}' is not a valid project name")
    if not _VERSION.match(version):
        raise ValueError(f"'{version}' is not a valid version")
    if not _REPOSITORY.match(repository):
        raise ValueError(f"'{repository}' is not an OWNER/REPO repository")
    if not _FULL_SHA.match(commit):
        raise ValueError(f"'{commit}' is not a full commit SHA")
    if not _WORKFLOW.match(workflow):
        raise ValueError(f"'{workflow}' is not a workflow file name")
    built: dict[str, Path] = {}
    for path in files:
        if not _FILENAME.match(path.name) or not path.is_file() or path.is_symlink():
            raise ValueError(f"{path} is not a regular distribution file")
        if path.name in built:
            raise ValueError(f"{path.name} is named twice")
        built[path.name] = path
    if not built:
        raise ValueError("no files to compare")
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in built.items()}

    project = re.sub(r"[-_.]+", "-", project).lower()
    label = f"{project} {version}"
    for attempt in range(attempts):
        raw = get(f"{_PYPI}/pypi/{project}/{version}/json", "application/json")
        served = None if raw is None else _served_files(raw, label)
        if served is not None and (set(served) - set(digests) or not set(digests) - set(served)):
            break
        if attempt + 1 < attempts:
            sleep(delay)
    if served is None:
        raise ValueError(f"PyPI does not serve {label}")
    if extra := sorted(set(served) - set(digests)):
        raise ValueError(f"PyPI serves files for {label} that were not built: {extra}")
    if missing := sorted(set(digests) - set(served)):
        raise ValueError(f"PyPI does not serve these files for {label}: {missing}")
    for name in sorted(digests):
        if served[name] != digests[name]:
            raise ValueError(
                f"PyPI serves {name} with SHA-256 {served[name]}, built {digests[name]}"
            )

    with tempfile.TemporaryDirectory() as scratch:
        for name in sorted(built):
            raw = get(f"{_PYPI}/integrity/{project}/{version}/{name}/provenance", _INTEGRITY_ACCEPT)
            if raw is None:
                raise ValueError(f"PyPI serves no provenance for {name}")
            _check_provenance(raw, name, repository, commit, workflow)
            provenance = Path(scratch) / f"{name}.provenance"
            provenance.write_bytes(raw)
            verify(built[name], provenance, repository)
    print(
        f"verify_pypi: PyPI serves {label} as built, with provenance from "
        f"{repository} at {commit}: {', '.join(sorted(built))}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--file", type=Path, action="append", required=True)
    arguments = parser.parse_args(argv)
    try:
        verify_publication(
            arguments.project,
            arguments.version,
            arguments.file,
            repository=arguments.repository,
            commit=arguments.commit,
            workflow=arguments.workflow,
        )
    except ValueError as error:
        print(f"verify_pypi: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
