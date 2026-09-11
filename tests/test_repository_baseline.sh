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
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "fa18b8f1272681a83062c370d846ca8a96cc4bcc6ede6441f19ac34e97d9fd40",
    ".github/workflows/ci.yml": "8a1b9db40abcf85694e38d4773bd5be908e5b8dbc37c4cbe5c426a95930b13c5",
    ".github/workflows/codeql.yml": "0d01772f298332e096e1c7508768f4417548e2f6ac8f6fb847aea47171c76405",
    ".github/workflows/no-ai-attribution.yml": "14e7f29c0a822ba936482363161e69388ba299711f9b34a60a51333c472dad54",
    ".github/workflows/publish-archives.yml": "0748621dfb8f3b7535c69875c26e7d73c72d8bafc1b96a8b5484298eea47b18c",
    ".github/workflows/release-archive.yml": "5c5cd461edc181dd43a0ab785c1cb00dbb728f1d2744a97ee8273a0a205db6c3",
    ".github/workflows/release-python.yml": "ca0db69a54f3c55d5a75ab13fc823e2d8854528510a982a1363e794f1bf361df",
    ".github/workflows/release-skills.yml": "26246f4ad1575a90c776fcd71bcfbca3daa6e9604fc5601d974b40783fab7d7a",
    ".github/workflows/verify-skills.yml": "f9fb4fa7560eb561b2cc34fd58acbfe2e52440e10ae25a9fde67dad997d18941",
    "README.md": "e97a4e31016c8ab9b77346b83e44c04e74f4fe904f6b2c75288a6d00120e5c82",
    "docs/python-consumers.md": "c31c0e85cdfcfc7e0cea1422a388cd16f5dc17070db9d22775bbea1da265840a",
    "docs/pypi-publishing.md": "d371728da74e3308fa18304240e2ffbf6323412c28e5c3d525cb325550cef3b4",
    "docs/archive-consumers.md": "16730282b48c25aa522b3bd6cf6b9be6f6fa0ed63762c69c8dd16b30bb79661b",
    "docs/skill-consumers.md": "bf410ecab21c23bf4a51331c4eb62fd6f566310bf9cd85ce0fc527ffa8823cfa",
    "docs/consumer-prerequisites.md": "0843bf113cbe6963cce05ddfdd447cd75c7ed847dd6c7a5c9cb6fe868d980335",
    "docs/guarantees-and-evidence.md": "c2044a7a26be229c20e660d2486a2800dd1b51e8d7b4eb5df12587224d59e60d",
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
