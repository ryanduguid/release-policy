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
    "docs/release-preflight.md": "cd8792460abe261325180c9db3f3e6e3d95f1b8cc294ebd990bc6693dcac570e",
    "docs/attribution-policy-stage2.yml": "e8e9921e796bb308117778d8444b21e01f8d6b04a45bc37ad49a3a8f18b2fde2",
    ".github/workflows/pr-review-central.yml": "3971be99df9010db6e9f9f6b9de8b06e86c5975270f684978e850d65ccca8a63",
    ".github/workflows/pr-review-trigger.yml": "8688d961b089d11b1056489ba5d407b3adb6a56d875253a800eb543f1bbb8bfe",
    ".github/workflows/pr-review-consumer.yml": "d8c91be83d74ccaacf309dd9cd2743e25db07bee63fd51ce44ca0b86ac415e61",
    ".github/pr-review-build-wheels.txt": "e75343a5ed6ddcd4f3c9fdb52e8a635fd52c4509dca5f0f22d2b29b0d22c9a18",
    "docs/pr-review.md": "4566bf335edb09a30839550519e4b062b18698e8e934035428cd9e148264bf93",
    ".github/pr-review-policy.json": "44036e011119babc6d6bea0e5ac9a98b05770f73cc8bc8a9d42844c7da810bba",
    ".github/workflows/pr-review.yml": "d161d02a0596fb8fbe83b5661f4713f77dc03f8f7f2b3eb423153a9494391700",
    "docs/attribution-consumers.md": "cd58487fb917a082c4b0b73139b35824f6be4aa0cd88a0d13ed002fb050af435",
    ".github/ci/check_gates.py": "eda4b878cde2c655247ef3b4bf388cf72d875506316d4d2c882c76a0bb85ecd2",
    ".github/actions/no-ai-attribution/action.yml": "7c3febb2fd89f92ead53571835dae1d1d20b2ce49eb127298993a1495b64e47f",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "61862ed90d98b35bd357e9bf55464003ecc3b1c3643530c593d12e0f666bfd80",
    ".github/workflows/ci.yml": "f93f5f27c08052344f4a59cb382a5210af1723d6ca41e5a29340ccf415148831",
    ".github/workflows/codeql.yml": "775b1946fa07e027de36f33dd022959f37575d16d0fc505605e7cbc575b67b54",
    ".github/workflows/scorecard.yml": "4aa63b8023ed49379052060ce6472323a997490973d8c11f48545705a0f5a8f6",
    ".github/workflows/snyk.yml": "64a0ec070bb17ad68409915d6ac3612e55c06ee4b7e5b4434eb7bd3a3f9a533b",
    ".github/workflows/no-ai-attribution.yml": "2c517d6f7eda8667c8a2eba186ef15d9e27b4c91d4617a39083b0a31f26f2834",
    ".github/workflows/attribution-policy.yml": "55eb47f6abbd0c08297bca7edf1c751193f537906092c3f4f4be0d07a1c5b3b9",
    ".github/workflows/publish-archives.yml": "1fcfd0146182c6ee2f2edcd854d5300c5ab970337579d445cdcab6d2629b59ca",
    ".github/workflows/release-archive.yml": "f48381cd714886a0f9dca8b532f4d7a1316ee2d16887131270fb03775f741483",
    ".github/workflows/release-python.yml": "d6d078b210581dc29749052570b604421afced78db7e651b7ec230cb20872377",
    ".github/workflows/release-skills.yml": "76a7f1996881def9f6f58e4ffdbe7a5a39557aa0e333fc445d6850be05ed2e81",
    ".github/workflows/verify-skills.yml": "6b3fc08d2a35bb65a1b61dc58df1c73a3b067ba1997c87b908342fa7d53fff18",
    ".github/workflows/virustotal-release.yml": "aff55c0d52905d575e9ec745582de013530bcae5e75a0df0a6d89c60657abaf0",
    "README.md": "6bd4a7f27470b2717e7eba58e8bec6b135880290b80f9d96c466edba0aa859a1",
    "docs/python-consumers.md": "f4a026b263cd7fdc706546249f812da336ca2b89db0ba7abbf3aca60546add9c",
    "docs/pypi-publishing.md": "bc23812fba6cac724d8a109e9b5a070b755333cd9d2ba75407537b00a2d024b7",
    "docs/archive-consumers.md": "6f455e3fa4eaf4ecc8cf773e58da2a81d2eb715b1e364496dab6628a0d11e4b5",
    "docs/skill-consumers.md": "db6d8ae9d72582cf472d60afa2cc7d5313740d2a52f06ef9f21d749c2d701456",
    "docs/consumer-prerequisites.md": "d2bf47cced7633f3b4f9626883556a70edba6bcbec770918b8bdd24823ea63a0",
    "docs/guarantees-and-evidence.md": "bae944dbfab0c8a25793a9a034bd929f0f187f8a47f468e01adba58a247efa79",
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
    elif name in {"stage-two-draft", "release-preflight"}:
        path = "docs/attribution-policy-stage2.yml" if name == "stage-two-draft" else "docs/release-preflight.md"
        documents[path] = bytes([documents[path][0] ^ 1]) + documents[path][1:]
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
    "stage-two-draft",
    "release-preflight",
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
