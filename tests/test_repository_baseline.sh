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
    ".gitattributes": "15950beebac9cc61cd4ee661d408e3f7e132c92e5d5f1f3fc0a27c6958eb70c4",
    ".github/dependabot.yml": "fa18b8f1272681a83062c370d846ca8a96cc4bcc6ede6441f19ac34e97d9fd40",
    ".github/workflows/ci.yml": "1b0a2611a2d6dd38228ae8a96b9bb6ffa269128864484b6154f1cded21826ff5",
    ".github/workflows/codeql.yml": "52ea291e1f5acb778ecd21e2b59760af5b41d2259a8d1c4fd456ef7e557e5381",
    ".github/workflows/publish-archives.yml": "7de1130f4f051575031f2d64d37ce44246c63efb62ac2e862c0153ac018aff8c",
    ".github/workflows/release-archive.yml": "5c5cd461edc181dd43a0ab785c1cb00dbb728f1d2744a97ee8273a0a205db6c3",
    ".github/workflows/release-python.yml": "0238b14687361faf023e0e1012c77feb693c4f61a520da98890b5bc88d1ede95",
    ".github/workflows/release-skills.yml": "26246f4ad1575a90c776fcd71bcfbca3daa6e9604fc5601d974b40783fab7d7a",
    ".github/workflows/verify-skills.yml": "f9fb4fa7560eb561b2cc34fd58acbfe2e52440e10ae25a9fde67dad997d18941",
    "README.md": "2978d9d4dbd7e007eb4bd6dce686a5ff2598fdabfb3fcd2e4a3556b478e05873",
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


def replace_once(text: str, old: str, new: str, name: str) -> str:
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"{name}: expected one literal match, found {count}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, replacement: str, name: str) -> str:
    changed, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE | re.DOTALL)
    if count != 1:
        raise AssertionError(f"{name}: expected one regular-expression match, found {count}")
    return changed


def mutate(canonical: dict[str, bytes], name: str) -> dict[str, bytes]:
    documents = dict(canonical)

    def text(path: str) -> str:
        return documents[path].decode("utf-8")

    def set_text(path: str, value: str) -> None:
        documents[path] = value.encode("utf-8")

    dependabot = ".github/dependabot.yml"
    ci = ".github/workflows/ci.yml"
    codeql = ".github/workflows/codeql.yml"
    readme = "README.md"
    security = "SECURITY.md"

    if name == "dependabot-extra-quoted-key":
        set_text(
            dependabot,
            text(dependabot)
            + '  - "package-ecosystem": npm\n'
            + "    directory: /\n"
            + "    schedule:\n"
            + "      interval: daily\n",
        )
    elif name == "dependabot-prepended-quoted-key":
        set_text(
            dependabot,
            replace_once(
                text(dependabot),
                "updates:\n",
                'updates:\n  - "package-ecosystem": npm\n    directory: /\n'
                "    schedule:\n      interval: daily\n",
                name,
            ),
        )
    elif name == "dependabot-extra-flow-entry":
        set_text(
            dependabot,
            text(dependabot)
            + "  - {package-ecosystem: npm, directory: /, schedule: {interval: daily}}\n",
        )
    elif name == "dependabot-duplicate-ecosystem-key":
        set_text(
            dependabot,
            replace_once(
                text(dependabot),
                "  - package-ecosystem: github-actions\n",
                "  - package-ecosystem: github-actions\n    package-ecosystem: npm\n",
                name,
            ),
        )
    elif name == "dependabot-quoted-scalars":
        value = text(dependabot)
        for old, new in (
            ("package-ecosystem: github-actions", 'package-ecosystem: "github-actions"'),
            ("directory: /\n", 'directory: "/"\n'),
            ("interval: weekly", 'interval: "weekly"'),
            ("package-ecosystem: pip", 'package-ecosystem: "pip"'),
            ("directory: /fixtures/demo-pkg", 'directory: "/fixtures/demo-pkg"'),
            ("interval: monthly", 'interval: "monthly"'),
        ):
            value = replace_once(value, old, new, name)
        set_text(dependabot, value)
    elif name == "dependabot-anchor":
        set_text(dependabot, text(dependabot) + "canonical-alias: &canonical-alias\n  interval: daily\n")
    elif name == "ci-extra-quoted-uses":
        set_text(
            ci,
            replace_once(
                text(ci),
                "      - name: Run gate tests",
                "      - name: Movable action through quoted key\n"
                '        "uses": actions/checkout@main\n\n'
                "      - name: Run gate tests",
                name,
            ),
        )
    elif name == "ci-extra-flow-uses":
        set_text(
            ci,
            replace_once(
                text(ci),
                "      - name: Run gate tests",
                "      - {name: Flow action, uses: actions/checkout@main}\n\n"
                "      - name: Run gate tests",
                name,
            ),
        )
    elif name == "ci-extra-job-quoted-write-all":
        set_text(
            ci,
            text(ci)
            + "\n  quoted-permission-job:\n"
            + '    "permissions": write-all\n'
            + "    runs-on: ubuntu-latest\n"
            + "    steps:\n"
            + "      - run: echo quoted permission key\n",
        )
    elif name == "workflow-documentation-sha-marker":
        set_text(
            ci,
            replace_once(
                text(ci),
                "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "uses: actions/checkout@<full-40-char-commit-sha>",
                name,
            ),
        )
    elif name == "workflow-extra-file":
        documents[".github/workflows/extra.yml"] = (
            b"name: Extra\non: push\njobs:\n  extra:\n    runs-on: ubuntu-latest\n"
        )
    elif name == "codeql-language-hidden-in-comment":
        set_text(
            codeql,
            replace_once(
                text(codeql),
                "language: [python, actions]",
                "language: [python]\n          # language: [python, actions]",
                name,
            ),
        )
    elif name == "codeql-schedule-hidden-in-comment":
        set_text(
            codeql,
            replace_once(
                text(codeql),
                'cron: "5 6 * * 1"',
                'cron: "5 6 * * 2"\n    # cron: "5 6 * * 1"',
                name,
            ),
        )
    elif name == "codeql-dispatch-hidden-in-comment":
        set_text(
            codeql,
            replace_once(text(codeql), "  workflow_dispatch:", "  # workflow_dispatch:", name),
        )
    elif name == "security-fabricated-only-in-comment":
        set_text(
            security,
            replace_once(
                text(security),
                "Use fabricated or redacted evidence only.",
                "<!-- Use fabricated or redacted evidence only. -->",
                name,
            ),
        )
    elif name == "security-authority-only-in-fence":
        authority = (
            "A security report does not authorise anyone to run a release, create a tag,\n"
            "publish an asset, use consumer credentials, or disclose the concern publicly."
        )
        set_text(
            security,
            replace_once(text(security), authority, f"```text\n{authority}\n```", name),
        )
    elif name == "security-natural-client-contradiction":
        set_text(
            security,
            replace_once(
                text(security),
                "A security report does not authorise",
                "Actual customer records and deployment keys are acceptable evidence.\n\n"
                "A security report does not authorise",
                name,
            ),
        )
    elif name == "security-natural-authority-contradiction":
        set_text(
            security,
            replace_once(
                text(security),
                "A security report does not authorise",
                "A maintainer may treat a report as permission to publish a release asset.\n\n"
                "A security report does not authorise",
                name,
            ),
        )
    elif name == "security-natural-sla":
        set_text(
            security,
            replace_once(
                text(security),
                "description, safe reproduction steps, impact, and any suggested mitigation.",
                "description, safe reproduction steps, impact, and any suggested mitigation.\n\n"
                "We guarantee a response in one week.",
                name,
            ),
        )
    elif name == "security-private-route-contradiction":
        set_text(
            security,
            replace_once(
                text(security),
                "Include a clear\ndescription",
                "If the form is absent, file the full details in an ordinary issue.\n\n"
                "Include a clear\ndescription",
                name,
            ),
        )
    elif name == "readme-release-body-only-in-comment":
        set_text(
            readme,
            regex_once(
                text(readme),
                r"(## Release status\n\n)(.*?)(\n## Using the packaged-Python release workflow)",
                r"\1<!--\n\2\n-->\3",
                name,
            ),
        )
    elif name == "readme-release-body-only-in-fence":
        set_text(
            readme,
            regex_once(
                text(readme),
                r"(## Release status\n\n)(.*?)(\n## Using the packaged-Python release workflow)",
                r"\1```text\n\2\n```\3",
                name,
            ),
        )
    elif name.startswith("readme-natural-"):
        contradictions = {
            "readme-natural-distributable-contradiction":
                "The tag is ready for installation as an officially verified distribution.",
            "readme-natural-rewrite-contradiction":
                "Maintainers may update this marker and attach artefacts later.",
            "readme-natural-phase-claim":
                "The source-archive phase is complete and available.",
            "readme-natural-immutability-contradiction":
                "The release may be made immutable before checksum verification.",
        }
        set_text(
            readme,
            replace_once(
                text(readme),
                "The repository now contains",
                contradictions[name] + "\n\nThe repository now contains",
                name,
            ),
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
    "dependabot-extra-quoted-key",
    "dependabot-prepended-quoted-key",
    "dependabot-extra-flow-entry",
    "dependabot-duplicate-ecosystem-key",
    "dependabot-quoted-scalars",
    "dependabot-anchor",
    "ci-extra-quoted-uses",
    "ci-extra-flow-uses",
    "ci-extra-job-quoted-write-all",
    "workflow-documentation-sha-marker",
    "workflow-extra-file",
    "codeql-language-hidden-in-comment",
    "codeql-schedule-hidden-in-comment",
    "codeql-dispatch-hidden-in-comment",
    "security-fabricated-only-in-comment",
    "security-authority-only-in-fence",
    "security-natural-client-contradiction",
    "security-natural-authority-contradiction",
    "security-natural-sla",
    "security-private-route-contradiction",
    "readme-release-body-only-in-comment",
    "readme-release-body-only-in-fence",
    "readme-natural-distributable-contradiction",
    "readme-natural-rewrite-contradiction",
    "readme-natural-phase-claim",
    "readme-natural-immutability-contradiction",
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
