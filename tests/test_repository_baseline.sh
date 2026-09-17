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
    "docs/attribution-consumers.md": "ce98729896c4fecef449039cdd750f2b7698fe7dc3d1893f57b4f39b51ec32b9",
    ".github/actions/no-ai-attribution/action.yml": "735528a813e1598aed2fbf41173a1fdc0c8359f7e620b0a984acaa77cf827196",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "baf5bed4e5b960787d53ee5ea8d9d7376fbc54565d6a392604fb94afb6bc1b66",
    ".github/workflows/ci.yml": "c4bfb79c6254f0d365bdd9db117ebbf36e283bce27dd282728899b8223a968b7",
    ".github/workflows/codeql.yml": "0d01772f298332e096e1c7508768f4417548e2f6ac8f6fb847aea47171c76405",
    ".github/workflows/no-ai-attribution.yml": "2c517d6f7eda8667c8a2eba186ef15d9e27b4c91d4617a39083b0a31f26f2834",
    ".github/workflows/attribution-policy.yml": "cf0a5e5989ddbfe5f742d383180636b5336ece895ffb76f69fd6e0a27f7134ce",
    ".github/workflows/publish-archives.yml": "0748621dfb8f3b7535c69875c26e7d73c72d8bafc1b96a8b5484298eea47b18c",
    ".github/workflows/release-archive.yml": "86da36d16073d91c138ff9dc2777b1bb0fae5a6d8e2d8b221a3bdbeed429f39f",
    ".github/workflows/release-python.yml": "f8ce82748fc5ffa5728d4c48dc21d25b25859b094c91f4a9d7f75979bdfce372",
    ".github/workflows/release-skills.yml": "999b6150a9844f4c071aeacb705e73a8c2701cc46dbdc2f475ab3a7278537561",
    ".github/workflows/verify-skills.yml": "38643ef4697d2a2c8820b20953b16372391563e16eea6ccf36c770f4e1749357",
    "README.md": "5ebb962cb7cb28e3928c8da1b8d1049695f8741fdf5cd2f1a615ca64fc519f6d",
    "docs/python-consumers.md": "1d0d64ba6c8ce00c7b58e01c7e98e1da5d2d3739351498d7c337d04477664962",
    "docs/pypi-publishing.md": "aac286a9fb1352077ee1435f06e21dee23760161b12dc5dc6cb8fa1193ca46a2",
    "docs/archive-consumers.md": "b3711c56f06bcd80e81f8e20f6fd3324016b9bb8fd1626b722e8d8241d4f2bd3",
    "docs/skill-consumers.md": "021d706c39947a582e3d80a42d92f2f80e548fa4e4f606cd71e51b676cfc76eb",
    "docs/consumer-prerequisites.md": "65960f5c83141a69fa34ae75d99c7edad63187db9e5598ce070a759366328d02",
    "docs/guarantees-and-evidence.md": "36e8de733efbafd9da776ad5c1742c571f7ae7d3a27f4609700584a89ee09a11",
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
