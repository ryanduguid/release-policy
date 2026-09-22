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
    "docs/attribution-consumers.md": "b3be8f0e4122bc0a9bc67b6ee7ed1eaeccf4a899bb27d174dedd689897567679",
    ".github/actions/no-ai-attribution/action.yml": "976d2207ea576ba28a742058930863ad51f41df1b5f25dfd7cf49b1cca212684",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "baf5bed4e5b960787d53ee5ea8d9d7376fbc54565d6a392604fb94afb6bc1b66",
    ".github/workflows/ci.yml": "ff003034bfa9d17d98f608627a9ecd9e7f2e4a1c0eaf42c2611ab773bb3b5cf1",
    ".github/workflows/codeql.yml": "0d01772f298332e096e1c7508768f4417548e2f6ac8f6fb847aea47171c76405",
    ".github/workflows/no-ai-attribution.yml": "2c517d6f7eda8667c8a2eba186ef15d9e27b4c91d4617a39083b0a31f26f2834",
    ".github/workflows/attribution-policy.yml": "39c090e9127aa2cacbecb04ae9227cbaade53ef9eb5640a4f3d367c33559e234",
    ".github/workflows/publish-archives.yml": "0748621dfb8f3b7535c69875c26e7d73c72d8bafc1b96a8b5484298eea47b18c",
    ".github/workflows/release-archive.yml": "86da36d16073d91c138ff9dc2777b1bb0fae5a6d8e2d8b221a3bdbeed429f39f",
    ".github/workflows/release-python.yml": "f8ce82748fc5ffa5728d4c48dc21d25b25859b094c91f4a9d7f75979bdfce372",
    ".github/workflows/release-skills.yml": "da761db3d202873201aded93e954c8222274153c8d289ebce08e7069bf74d915",
    ".github/workflows/verify-skills.yml": "38643ef4697d2a2c8820b20953b16372391563e16eea6ccf36c770f4e1749357",
    "README.md": "5ebb962cb7cb28e3928c8da1b8d1049695f8741fdf5cd2f1a615ca64fc519f6d",
    "docs/python-consumers.md": "2c80ac89e6d98c9c077865592883f96eaed72f9b27fd40c7c055ce27c4e33166",
    "docs/pypi-publishing.md": "a4f529f5e35c966dfbf014faf461895047a66294057581fe14f3501c4f84062e",
    "docs/archive-consumers.md": "4e0693916a88f51edffb980b3ae343bc09c67475d608b3939d8a8e554a9478ed",
    "docs/skill-consumers.md": "1bd40fdd13f6feafa077f83ed019ba8296a64eb698d9a18ca42a5d3651760146",
    "docs/consumer-prerequisites.md": "222f5ffe3a9e8efc14a60c370e7df91364aff396b2ffeee1e286cb7aece364bc",
    "docs/guarantees-and-evidence.md": "2789407bc0d0ed3d03e68c3960ff2cf5953daaa8ebcef0205fccc31fefb721cb",
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
