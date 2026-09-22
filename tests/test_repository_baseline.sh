#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${REPOSITORY_ROOT:-$HERE/..}"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

"$PYTHON" - "$ROOT" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


root = Path(sys.argv[1]).resolve()

# These digests are the single canonical contract for the reviewed policy and
# workflow documents. Keeping action pins and permission maps inside the exact
# workflow documents avoids a second, drift-prone representation here.
CANONICAL_SHA256 = {
    "docs/attribution-consumers.md": "11eafbd75f83686cee26b7b7e4a9b4d7a43d5625451de933291b112027336e7b",
    ".github/actions/no-ai-attribution/action.yml": "156d044f0b8b7df6c28b0b061234cd012096d77e4b3fe8dd1eb1a92d71a6fec2",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "baf5bed4e5b960787d53ee5ea8d9d7376fbc54565d6a392604fb94afb6bc1b66",
    ".github/workflows/ci.yml": "eb56ff150b4d58dc6165cc47b76ab3996f3977aef8025f351d7f2eecc656a0b4",
    ".github/workflows/codeql.yml": "5ec5aa2e0e88fdad23a34b136cbea137e784bc4c078b19af116e7a3c4f923670",
    ".github/workflows/no-ai-attribution.yml": "2c517d6f7eda8667c8a2eba186ef15d9e27b4c91d4617a39083b0a31f26f2834",
    ".github/workflows/attribution-policy.yml": "39c090e9127aa2cacbecb04ae9227cbaade53ef9eb5640a4f3d367c33559e234",
    ".github/workflows/publish-archives.yml": "cf9bbe9224780c15b72ef53915da962e20bcf59468295bb6da0bfccb9ceecf10",
    ".github/workflows/release-archive.yml": "df97930ad8fabaf270a925167c7430e4c70226e91f852788efb2d8c374bbe851",
    ".github/workflows/release-python.yml": "a116c6779952e18dffa278f74093c781f0b6585c89d8cb82d459463fcd695aa4",
    ".github/workflows/release-skills.yml": "54edcf783dbe55050c49d10356be5f32202697c29527b64210d1c739e98059c8",
    ".github/workflows/verify-skills.yml": "38643ef4697d2a2c8820b20953b16372391563e16eea6ccf36c770f4e1749357",
    "README.md": "5ebb962cb7cb28e3928c8da1b8d1049695f8741fdf5cd2f1a615ca64fc519f6d",
    "docs/python-consumers.md": "2c80ac89e6d98c9c077865592883f96eaed72f9b27fd40c7c055ce27c4e33166",
    "docs/pypi-publishing.md": "419a92d216cde938ab0f0711a3d1fca56842396ca973058a4ca3796a6068b694",
    "docs/archive-consumers.md": "4e0693916a88f51edffb980b3ae343bc09c67475d608b3939d8a8e554a9478ed",
    "docs/skill-consumers.md": "1bd40fdd13f6feafa077f83ed019ba8296a64eb698d9a18ca42a5d3651760146",
    "docs/consumer-prerequisites.md": "05c160dfd2cb4797e046a88f7bc6d850db87e9b4b29f070e63b7284bfdf9c90d",
    "docs/guarantees-and-evidence.md": "55591352b70163ddf1af06eac5712c24cd5b15a02941a37188b3284fc4ec6038",
    "docs/egress-check.md": "2d8f86aad40c65edec23d370cbfb39864cf5a101d24839055c7b2718592f5994",
    "SECURITY.md": "9e7f9e17cf7c23e350ff08fbf25ff14a2e17071fad9b1b1d57c91a4a2e834594",
}


def normalise(raw: bytes, path: str) -> tuple[bytes | None, str | None]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return None, f"{path}: UTF-8 BOM is not canonical"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, f"{path}: document is not valid UTF-8"
    if "\r" in text.replace("\r\n", ""):
        return None, f"{path}: lone carriage return is not canonical"
    return text.replace("\r\n", "\n").encode("utf-8"), None


def validate(documents: dict[str, bytes]) -> list[str]:
    failures: list[str] = []
    for path in sorted(set(documents) - set(CANONICAL_SHA256)):
        failures.append(f"{path}: unexpected workflow document is outside the canonical set")
    for path, expected in CANONICAL_SHA256.items():
        raw = documents.get(path)
        if raw is None:
            failures.append(f"{path}: required canonical document is missing")
            continue
        canonical, error = normalise(raw, path)
        if error:
            failures.append(error)
            continue
        actual = hashlib.sha256(canonical).hexdigest()
        if actual != expected:
            failures.append(
                f"{path}: canonical content mismatch "
                f"(expected {expected}, found {actual})"
            )
    return failures


def load_documents() -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    for path in CANONICAL_SHA256:
        try:
            documents[path] = (root / path).read_bytes()
        except FileNotFoundError:
            pass
    workflow_root = root / ".github" / "workflows"
    if workflow_root.is_dir():
        for workflow in workflow_root.iterdir():
            if workflow.is_file() and workflow.suffix.casefold() in {".yml", ".yaml"}:
                path = workflow.relative_to(root).as_posix()
                documents.setdefault(path, workflow.read_bytes())
    return documents


def mutate(canonical: dict[str, bytes], name: str) -> dict[str, bytes]:
    documents = dict(canonical)
    if name == "dependabot-anchor":
        documents[".github/dependabot.yml"] += (
            b"canonical-alias: &canonical-alias\n  interval: daily\n"
        )
    elif name == "workflow-extra-file":
        documents[".github/workflows/extra.yml"] = (
            b"name: Extra\non: push\njobs:\n  extra:\n    runs-on: ubuntu-latest\n"
        )
    else:
        raise AssertionError(f"unknown mutation: {name}")
    return documents


documents = load_documents()
failures = validate(documents)
if failures:
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    print(f"repository baseline failed: {len(failures)} failure(s)", file=sys.stderr)
    raise SystemExit(1)

canonical_lf: dict[str, bytes] = {}
for path, raw in documents.items():
    canonical, error = normalise(raw, path)
    if error:
        raise AssertionError(error)
    canonical_lf[path] = canonical

self_test_failures: list[str] = []
if validate(canonical_lf):
    self_test_failures.append("canonical LF control was rejected")
canonical_crlf = {path: raw.replace(b"\n", b"\r\n") for path, raw in canonical_lf.items()}
if validate(canonical_crlf):
    self_test_failures.append("canonical CRLF control was rejected")

adverse = (
    "dependabot-anchor",
    "workflow-extra-file",
)
for name in adverse:
    if not validate(mutate(canonical_lf, name)):
        self_test_failures.append(f"adverse mutation was accepted: {name}")

if self_test_failures:
    for failure in self_test_failures:
        print(f"FAIL self-test: {failure}", file=sys.stderr)
    print(
        f"repository baseline self-test failed: {len(self_test_failures)} failure(s)",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"repository baseline passed: {len(CANONICAL_SHA256)} canonical documents; "
    f"LF/CRLF controls and {len(adverse)} adverse variants"
)
PY
