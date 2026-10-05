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
    ".github/workflows/pr-review-central.yml": "6f0086664fbafebbba915133fdf631f6f3868738e8caeb6e2509f8eafd87b344",
    ".github/workflows/pr-review-trigger.yml": "6f2da6fecf622d02643f0d97172abe24c1f009049a09da63fea2a34f87783542",
    ".github/workflows/pr-review-consumer.yml": "d8c91be83d74ccaacf309dd9cd2743e25db07bee63fd51ce44ca0b86ac415e61",
    ".github/workflows/pr-review-event-probe.yml": "ea49c18f142c4cf93063612c9c01820d29e8e795c2ea5c32cefd381fc17f1a02",
    ".github/pr-review-build-wheels.txt": "e75343a5ed6ddcd4f3c9fdb52e8a635fd52c4509dca5f0f22d2b29b0d22c9a18",
    "docs/pr-review.md": "0811b64ebc538b296ea7ee14a38f885b8aeffe7cf4ec0f6bb3c496e089e7a444",
    ".github/pr-review-policy.json": "44036e011119babc6d6bea0e5ac9a98b05770f73cc8bc8a9d42844c7da810bba",
    ".github/workflows/pr-review.yml": "06ba109cad8328cabed418d1e3e0cedc7b279b623c2f8ff6558c09e9ddaeeede",
    "docs/attribution-consumers.md": "78f8fbc8be4cc07d0c407a5c4fe2eea746d528194dff457ecdcbde80d6c2ade9",
    ".github/ci/check_gates.py": "eda4b878cde2c655247ef3b4bf388cf72d875506316d4d2c882c76a0bb85ecd2",
    ".github/actions/no-ai-attribution/action.yml": "60c2e78c85bbe0320dc1d6a2e0218590928f52d1cd496cb8c52ea569a4e8a2e9",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "baf5bed4e5b960787d53ee5ea8d9d7376fbc54565d6a392604fb94afb6bc1b66",
    ".github/workflows/ci.yml": "dfa530efb3832fbb4c26e7fda07adaccbd66d903122e169e3500aba2fae7d4ec",
    ".github/workflows/codeql.yml": "74269f88ca9a8103a89351cdbbe61c025c1a9742c574a3cdef4c411a10cd8ba0",
    ".github/workflows/scorecard.yml": "32586611b864795d9ebf5f127005703f07214869d97a6e132c7932834e5f2626",
    ".github/workflows/no-ai-attribution.yml": "2c517d6f7eda8667c8a2eba186ef15d9e27b4c91d4617a39083b0a31f26f2834",
    ".github/workflows/attribution-policy.yml": "c956c865c80e77d87d4f86eadee8dca6c95991ae2452db96599e6a4b402b20b1",
    ".github/workflows/publish-archives.yml": "f85b24e289d1516bd9572b4516edc67b7aa5ef84a12c0db7cb724f234dfe2ce8",
    ".github/workflows/release-archive.yml": "e293a419e3949931d56b15b2604002db2990ae37d29dfdb30af531ebf8256715",
    ".github/workflows/release-python.yml": "a884408be5d8a279336a8bf7d0c86db670561839822e7e8dbf39420041499be7",
    ".github/workflows/release-skills.yml": "ba024dff4b9587833da59fae4e32b2df809ffea1198942461cf959978a294de0",
    ".github/workflows/verify-skills.yml": "113803d80bc8ab0d7bbb990cdbf10522865929a74a76aff33b8f5dd60d8e4bf0",
    "README.md": "27925f910b09d5652441289fbf834fbf4438d79bc8f32fa3a9c64048b705c37f",
    "docs/python-consumers.md": "2c80ac89e6d98c9c077865592883f96eaed72f9b27fd40c7c055ce27c4e33166",
    "docs/pypi-publishing.md": "419a92d216cde938ab0f0711a3d1fca56842396ca973058a4ca3796a6068b694",
    "docs/archive-consumers.md": "0e71c471994818790e54074c0c483053afcc9ffbccfac764c0e15e91ea17bfc7",
    "docs/skill-consumers.md": "1bd40fdd13f6feafa077f83ed019ba8296a64eb698d9a18ca42a5d3651760146",
    "docs/consumer-prerequisites.md": "369aa3c0fb8cd7a3f5f2cbceae5c3a85d2f08d673cada9a4ed3b745d1559b9de",
    "docs/guarantees-and-evidence.md": "55591352b70163ddf1af06eac5712c24cd5b15a02941a37188b3284fc4ec6038",
    "docs/egress-check.md": "2d8f86aad40c65edec23d370cbfb39864cf5a101d24839055c7b2718592f5994",
    "docs/re-pinning.md": "eb1b361cf717bcbabd383e0cf5592998fe5b045ae90982b0c6dd84eda626fe0f",
    "SECURITY.md": "9f9cea868cdb60a00c98ebaa892125d83a94fba1589d55227cd239c6af7804f9",
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
