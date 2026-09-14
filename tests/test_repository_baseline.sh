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
    "docs/attribution-consumers.md": "6744aad349574785ecdcc495b1b6b5f2370fa5f66bf3467ff827f000de43c37d",
    ".github/actions/no-ai-attribution/action.yml": "caab09af9770d353d0feb8819a8b4e19dd081e32ec9fe8d600fe3d70a17eb620",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "fa18b8f1272681a83062c370d846ca8a96cc4bcc6ede6441f19ac34e97d9fd40",
    ".github/workflows/ci.yml": "fae94abd26b5d639d4fa1c8336166d3ddb6d5559cdfeeee5476796f6da6ae3c3",
    ".github/workflows/codeql.yml": "0d01772f298332e096e1c7508768f4417548e2f6ac8f6fb847aea47171c76405",
    ".github/workflows/no-ai-attribution.yml": "3a41017245f9e8c3e41de2d828c1bb2e5b49721391a177905dceb861277a0f5b",
    ".github/workflows/attribution-policy.yml": "cf0a5e5989ddbfe5f742d383180636b5336ece895ffb76f69fd6e0a27f7134ce",
    ".github/workflows/publish-archives.yml": "0748621dfb8f3b7535c69875c26e7d73c72d8bafc1b96a8b5484298eea47b18c",
    ".github/workflows/release-archive.yml": "b1df7d5f2280e5efc131d0c686e81ba56841820fecc45b3fec8e1f35e91e05f3",
    ".github/workflows/release-python.yml": "ca0db69a54f3c55d5a75ab13fc823e2d8854528510a982a1363e794f1bf361df",
    ".github/workflows/release-skills.yml": "e38d76d63abe943ee516504dabb7de8105ff57a9e8424cd35b077ce58b41d729",
    ".github/workflows/verify-skills.yml": "38643ef4697d2a2c8820b20953b16372391563e16eea6ccf36c770f4e1749357",
    ".github/workflows/coderabbit-review-trigger.yml": "b5513eaef5ce239ea1b6087037b9b103832a438f241ee3b2233b1675dd4c70ea",
    "README.md": "2c06e0fe70e5693c65d2c2c9eea6599a2f1e33b9966a5956f27a894fd18364c6",
    "docs/python-consumers.md": "c31c0e85cdfcfc7e0cea1422a388cd16f5dc17070db9d22775bbea1da265840a",
    "docs/pypi-publishing.md": "aac286a9fb1352077ee1435f06e21dee23760161b12dc5dc6cb8fa1193ca46a2",
    "docs/archive-consumers.md": "e7f2557374467ff3431dd7fe702debc1be7836942288d28a314eda5f43bc21df",
    "docs/skill-consumers.md": "41eaea2a6f4ec707de902d0d68c8cf4b82c82f3458fcf21752de646eb4fea9f2",
    "docs/consumer-prerequisites.md": "b078b5f82d0f9eba7134ad12cf5edd04cb6db8f71746e72138bb05e9fd33b9dc",
    "docs/guarantees-and-evidence.md": "6ee2355356476a168b28959cf7ed3696a5fa6e62c1d7a4265edc975b00699e16",
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
