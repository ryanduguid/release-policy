#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${REPOSITORY_ROOT:-$HERE/..}"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

"$PYTHON" - "$ROOT" <<'PY'
from __future__ import annotations

import hashlib
import re
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
    ".github/workflows/pr-review.yml": "aaa85f96285e12f0872e44f4f047aac47b75c82a1af2e74170f67ea7c23dacad",
    "docs/attribution-consumers.md": "cd58487fb917a082c4b0b73139b35824f6be4aa0cd88a0d13ed002fb050af435",
    ".github/ci/check_gates.py": "eda4b878cde2c655247ef3b4bf388cf72d875506316d4d2c882c76a0bb85ecd2",
    ".github/actions/no-ai-attribution/action.yml": "b37c1426fcdc086e7063d5fdb6ac04a4581b1d2b78abefe5d6929a36c7801656",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "61862ed90d98b35bd357e9bf55464003ecc3b1c3643530c593d12e0f666bfd80",
    ".github/workflows/ci.yml": "35ae25053883bc19c5ec54527d518c445b2f8520b2efde46250c463d081b7ab0",
    ".github/workflows/codeql.yml": "447bc76406719470104571086cb35cec94e737bc1c01e45a74e0abd3fb1dc94d",
    ".github/workflows/scorecard.yml": "0b53690e51973336600e3b9d7f05f9e3c71c5de065bb3b306a447245f8644286",
    ".github/workflows/snyk.yml": "6e06196e83e1862cadb63c5729d3df61793cca4c87b4caa70b99ab62922a79eb",
    ".github/workflows/no-ai-attribution.yml": "2c517d6f7eda8667c8a2eba186ef15d9e27b4c91d4617a39083b0a31f26f2834",
    ".github/workflows/attribution-policy.yml": "55eb47f6abbd0c08297bca7edf1c751193f537906092c3f4f4be0d07a1c5b3b9",
    ".github/workflows/publish-archives.yml": "ce3bbebf8f50149f63e3519a0c48b0173dab0e962fc11f7245903999ae7b95b1",
    ".github/workflows/release-archive.yml": "0a3fdd6c0bb70b990ef82164c71028d4c4e28924268101fd7d8491cc3e39a4f4",
    ".github/workflows/release-python.yml": "ec5d42e4a1053ddd5605ce636f2bf03781ae03294b46315ba7008f04e5aac70f",
    ".github/workflows/release-skills.yml": "5934242cdd7bbd0dbad3cb73f8bc63eafcbf1e0aaea0231be0d27b87ff9c8d4d",
    ".github/workflows/verify-skills.yml": "3d6e51be78bf4cb0e22ce0dcf28bac5852e21daae34c3abff262d9a97f56de47",
    ".github/workflows/virustotal-release.yml": "fc3ec520ef37f4ded629fbab95a2bc1fd999ab98caced8b63cc813ca8e995a44",
    "README.md": "6bd4a7f27470b2717e7eba58e8bec6b135880290b80f9d96c466edba0aa859a1",
    "docs/python-consumers.md": "f4a026b263cd7fdc706546249f812da336ca2b89db0ba7abbf3aca60546add9c",
    "docs/pypi-publishing.md": "a2f8df87d92a6c43c293fc29922f3ea16f040517df63343545749fad204d2513",
    "docs/archive-consumers.md": "6f455e3fa4eaf4ecc8cf773e58da2a81d2eb715b1e364496dab6628a0d11e4b5",
    "docs/skill-consumers.md": "db6d8ae9d72582cf472d60afa2cc7d5313740d2a52f06ef9f21d749c2d701456",
    "docs/consumer-prerequisites.md": "d2bf47cced7633f3b4f9626883556a70edba6bcbec770918b8bdd24823ea63a0",
    "docs/guarantees-and-evidence.md": "bae944dbfab0c8a25793a9a034bd929f0f187f8a47f468e01adba58a247efa79",
    "docs/egress-check.md": "2d8f86aad40c65edec23d370cbfb39864cf5a101d24839055c7b2718592f5994",
    "docs/re-pinning.md": "eb1b361cf717bcbabd383e0cf5592998fe5b045ae90982b0c6dd84eda626fe0f",
    "SECURITY.md": "9f9cea868cdb60a00c98ebaa892125d83a94fba1589d55227cd239c6af7804f9",
}

# A third-party pin is hashed as action@<pin> # vMAJOR, so a Dependabot minor or
# patch bump moves no digest. The action, its path, the major version and every
# other byte stay pinned: a major bump, a new action or a pin without its version
# comment still needs a reviewed digest change. This repository's own pins are
# never normalised.
THIRD_PARTY_PIN = re.compile(
    rb"^(?P<head>[ \t]*(?:- )?uses: (?!(?i:ryanduguid)/)[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)"
    rb"@[0-9a-f]{40} # (?P<major>v[0-9]+)(?:\.[0-9]+){0,2}$",
    re.MULTILINE,
)


def digest(canonical: bytes) -> str:
    return hashlib.sha256(THIRD_PARTY_PIN.sub(rb"\g<head>@<pin> # \g<major>", canonical)).hexdigest()


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
        actual = digest(canonical)
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
    elif name.startswith("pin-"):
        path = ".github/workflows/ci.yml"
        pin = re.compile(rb"(uses: actions/checkout)@[0-9a-f]{40} # v([0-9]+)(?:\.[0-9]+){0,2}$", re.MULTILINE)
        edits = {
            "pin-bump": lambda m: m[1] + b"@" + b"0" * 40 + b" # v" + m[2] + b".99.0",
            "pin-action": lambda m: m[0].replace(b"actions/", b"actionz/"),
            "pin-major": lambda m: m[1] + b"@" + b"0" * 40 + b" # v%d.0.0" % (int(m[2]) + 1),
            "pin-comment": lambda m: m[1] + b"@" + b"0" * 40,
        }
        documents[path], count = pin.subn(edits[name], documents[path], count=1)
        if count != 1:
            raise AssertionError(f"{path}: no actions/checkout pin to mutate")
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
if validate(mutate(canonical_lf, "pin-bump")):
    self_test_failures.append("third-party minor pin bump control was rejected")
for owner in (b"ryanduguid", b"RyanDuguid"):
    own_pin = b"uses: " + owner + b"/release-policy/.github/workflows/pr-review.yml@%s # v1.0.0\n"
    if digest(own_pin % (b"a" * 40)) == digest(own_pin % (b"b" * 40)):
        self_test_failures.append(f"own-repository pin was normalised: {owner.decode()}")

adverse = (
    "dependabot-anchor",
    "workflow-extra-file",
    "stage-two-draft",
    "release-preflight",
    "pin-action",
    "pin-major",
    "pin-comment",
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
