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
    ".github/workflows/pr-review-consumer.yml": "4ea92f6b8a1439a43a91953fc14f078e463b2214c68f309efc17624e989ebf78",
    ".github/pr-review-build-wheels.txt": "51b0d44a3259e8186acbbc9caa1333f6179d88198c3feec932544a2cfdac9f25",
    "docs/pr-review.md": "9d3e0a4aac0ed0f945fdc2bee8fd7a99099e05ae8cb0d4dc38b1d631a2c230c6",
    ".github/pr-review-policy.json": "44036e011119babc6d6bea0e5ac9a98b05770f73cc8bc8a9d42844c7da810bba",
    ".github/workflows/pr-review.yml": "2e5e3775e4ad72fae2ad7e32bd49627bb93300df447a522b5aed1b1a73f27f25",
    "docs/attribution-consumers.md": "cd58487fb917a082c4b0b73139b35824f6be4aa0cd88a0d13ed002fb050af435",
    ".github/ci/check_gates.py": "8d48fa32c92d15bb476e28f2ad34400660f62ee337c527c1d9fd9b7f6230f62a",
    ".github/actions/no-ai-attribution/action.yml": "3e123ba40b6b76eb22cb289625d3ba5ca606907339172a00d86b331bb5847e93",
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "61862ed90d98b35bd357e9bf55464003ecc3b1c3643530c593d12e0f666bfd80",
    ".github/workflows/ci.yml": "d2c9901c86fcc376bcd87248ffb09f195dfc99bcb73492831651d48138a4e370",
    ".github/workflows/codeql.yml": "86a84c42a8de80f5c197c6ba7fd445a15734d6567d08add3d639baccf0e0835f",
    ".github/workflows/scorecard.yml": "0cfb357dd0c38ab298a1c761f83f8c087997b05b6038a838ee27655a9199780c",
    ".github/workflows/snyk.yml": "c200650b001ac55bec2e87059d7a223b374191a9b253a53e064364dd283fa0af",
    ".github/workflows/no-ai-attribution.yml": "2c517d6f7eda8667c8a2eba186ef15d9e27b4c91d4617a39083b0a31f26f2834",
    ".github/workflows/attribution-policy.yml": "82061e085c15dbe527c02294940d9966d4916b5d4f627f112b8b6ed092fd95b0",
    ".github/workflows/attribution-repin.yml": "310f3c875f502ce59a1ca9258fbc3275db44c16b16838cb02a1161c4247cf139",
    ".github/workflows/publish-archives.yml": "e84d2f2b0bc4593d4f3f1e43645504fac9ccf3a8459b3e89d83b118e77dc757f",
    ".github/workflows/release-archive.yml": "21dee8370a36f36d406a83197c2d42ce98d929eb87ce8bd086d740eca5faaac4",
    ".github/workflows/release-binary.yml": "9a1d611f415b4384cda830b1a359f7bddeb51ddf9d5a5fb127ac97b56a78ccbd",
    ".github/workflows/release-disposable-binary-canary.yml": "d087f3ff8b9b1c5950828974be1f30c840399d47e77794d241ef98b63e7e46be",
    ".github/workflows/release-python.yml": "072af978fe1628d081a2f804446d42c68b97cde0b8da8654acb2deaf64a5821d",
    ".github/workflows/release.yml": "07c678387fd5aa6a9c7f4334d4aaf0c93f63b6738e887ae25dca58179c97be15",
    ".github/workflows/release-skills.yml": "a4b57255f0cb40bcda7a8048c34497fb3259887ce0083fa93694943defa3f47f",
    ".github/workflows/verify-skills.yml": "45c46dbf4b06a17c13d4a2dbadd5bb8f790f28560ea43e78ac83bf9dea9683cc",
    ".github/workflows/virustotal-release.yml": "fc3ec520ef37f4ded629fbab95a2bc1fd999ab98caced8b63cc813ca8e995a44",
    "README.md": "d904328b03f362ed7f4617fc9b6726ea0f1d3e7665f8399ddae5266e02393e2a",
    "docs/python-consumers.md": "f4a026b263cd7fdc706546249f812da336ca2b89db0ba7abbf3aca60546add9c",
    "docs/pypi-publishing.md": "c04789514ed01b85fb06a9f515527e9b0363800cb36f964de1ed468257ab19ad",
    "docs/archive-consumers.md": "6f455e3fa4eaf4ecc8cf773e58da2a81d2eb715b1e364496dab6628a0d11e4b5",
    "docs/binary-consumers.md": "a06130ffb75cee65a57bde170617ad126759be5144e1e50cd632c203e08547bd",
    "docs/skill-consumers.md": "db6d8ae9d72582cf472d60afa2cc7d5313740d2a52f06ef9f21d749c2d701456",
    "docs/consumer-prerequisites.md": "d2bf47cced7633f3b4f9626883556a70edba6bcbec770918b8bdd24823ea63a0",
    "docs/guarantees-and-evidence.md": "bae944dbfab0c8a25793a9a034bd929f0f187f8a47f468e01adba58a247efa79",
    "docs/egress-check.md": "2d8f86aad40c65edec23d370cbfb39864cf5a101d24839055c7b2718592f5994",
    "docs/re-pinning.md": "b23ff2d1ba4c0053e4c703a162048b23f40817daba5235552bfcad87b78148f6",
    "SECURITY.md": "9f9cea868cdb60a00c98ebaa892125d83a94fba1589d55227cd239c6af7804f9",
}

# A third-party pin is hashed as action@<40 zeros> # vMAJOR, so a Dependabot minor
# or patch bump moves no digest. The action, its path, the major version and every
# other byte stay pinned: a major bump, a new action or a pin without its version
# comment still needs a reviewed digest change. The placeholder is itself a valid
# pin, so no unpinned line can hash the same. This repository's own pins and local
# ./ actions are never normalised.
THIRD_PARTY_PIN = re.compile(
    rb"^(?P<head>[ \t]*(?:- )?uses: (?!(?i:ryanduguid)/)[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_./-]+)"
    rb"@[0-9a-f]{40} # (?P<major>v[0-9]+)(?:\.[0-9]+){0,2}$",
    re.MULTILINE,
)


def digest(canonical: bytes) -> str:
    return hashlib.sha256(
        THIRD_PARTY_PIN.sub(rb"\g<head>@" + b"0" * 40 + rb" # \g<major>", canonical)
    ).hexdigest()


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
            "pin-bump": lambda m: m[1] + b"@" + b"1" * 40 + b" # v" + m[2] + b".99.0",
            "pin-action": lambda m: m[0].replace(b"actions/", b"actionz/"),
            "pin-major": lambda m: m[1] + b"@" + b"1" * 40 + b" # v%d.0.0" % (int(m[2]) + 1),
            "pin-comment": lambda m: m[1] + b"@" + b"1" * 40,
            "pin-placeholder": lambda m: m[1] + b"@<pin> # v" + m[2],
            "pin-trailing": lambda m: m[0] + b" # extra",
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
for action in (
    b"ryanduguid/release-policy/.github/workflows/pr-review.yml",
    b"RyanDuguid/release-policy/.github/workflows/pr-review.yml",
    b"./build/action",
):
    line = b"uses: " + action + b"@%s # v1.0.0\n"
    if digest(line % (b"a" * 40)) == digest(line % (b"b" * 40)):
        self_test_failures.append(f"pin was normalised: {action.decode()}")

adverse = (
    "dependabot-anchor",
    "workflow-extra-file",
    "stage-two-draft",
    "release-preflight",
    "pin-action",
    "pin-major",
    "pin-comment",
    "pin-placeholder",
    "pin-trailing",
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
