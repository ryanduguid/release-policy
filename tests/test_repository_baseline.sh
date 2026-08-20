#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${REPOSITORY_ROOT:-$HERE/..}"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

"$PYTHON" - "$ROOT" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path


root = Path(sys.argv[1]).resolve()
failures: list[str] = []
checks = 0


def check(condition: bool, label: str, detail: str) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(f"{label}: {detail}")


def read(relative_path: str) -> str:
    path = root / relative_path
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        failures.append(f"file {relative_path}: required file is missing")
    except UnicodeDecodeError:
        failures.append(f"file {relative_path}: file is not valid UTF-8")
    return ""


def markdown_section(document: str, heading: str, label: str) -> str:
    lines = document.splitlines()
    heading_pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.IGNORECASE)
    starts = [index for index, line in enumerate(lines) if heading_pattern.fullmatch(line)]
    check(
        len(starts) == 1,
        label,
        f"expected exactly one '## {heading}' section, found {len(starts)}",
    )
    if len(starts) != 1:
        return ""
    start = starts[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if re.match(r"^##\s+", lines[index]):
            end = index
            break
    return re.sub(r"\s+", " ", "\n".join(lines[start:end]).casefold()).strip()


def clean_yaml_line(line: str) -> str:
    if line.lstrip().startswith("#"):
        return ""
    return line.split(" #", 1)[0].rstrip()


def scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def nested_block(lines: list[str], start: int, parent_indent: int) -> list[str]:
    block: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= parent_indent:
            break
        block.append(line)
    return block


security = read("SECURITY.md")
supported = markdown_section(security, "Supported versions", "security supported versions")
reporting = markdown_section(
    security, "Reporting a vulnerability", "security vulnerability reporting"
)
safe_data = markdown_section(security, "Safe reporting data", "security safe data")

check(
    "latest" in supported and "default branch" in supported,
    "security supported versions",
    "support must target the latest default-branch revision",
)
check(
    "older commits" in supported and "historical tags" in supported,
    "security supported versions",
    "older commits and historical tags must not imply separate maintenance",
)
check(
    "update" in supported and "pin" in supported,
    "security supported versions",
    "consumers must be told to update their reviewed pin for a fix",
)
check(
    "v0.1.0" in supported and "not" in supported and "maintained" in supported,
    "security supported versions",
    "the notes-only marker must not be a separately maintained release line",
)

check(
    "private" in reporting
    and "report a vulnerability" in reporting
    and "security tab" in reporting,
    "security vulnerability reporting",
    "reporting must use GitHub's private Security-tab form",
)
check(
    "when" in reporting
    and "available" in reporting
    and "if" in reporting
    and ("absent" in reporting or "unavailable" in reporting),
    "security vulnerability reporting",
    "the private route must be conditional on live form availability",
)
check(
    "public issue" in reporting
    and "public discussion" in reporting
    and ("do not" in reporting or "never" in reporting),
    "security vulnerability reporting",
    "fallback wording must prohibit public issue and discussion disclosure",
)
check(
    all(term in reporting for term in ("description", "reproduction", "impact", "mitigation")),
    "security vulnerability reporting",
    "reports must request description, safe reproduction, impact and mitigation",
)

check(
    ("fabricated" in safe_data or "synthetic" in safe_data) and "redacted" in safe_data,
    "security safe data",
    "safe evidence must be fabricated or redacted",
)
check(
    all(
        term in safe_data
        for term in (
            "client data",
            "credentials",
            "tokens",
            ".env",
            "private keys",
            "workpapers",
            "release credentials",
        )
    ),
    "security safe data",
    "real client and secret-bearing material must be excluded",
)
check(
    re.search(
        r"(?:client data|credentials|tokens|secrets).{0,100}\b(?:may|can)\s+(?:be\s+)?(?:included|used|shared)",
        safe_data,
    )
    is None,
    "security safe data",
    "the safe-data section must not contradict its exclusions",
)
check(
    all(
        term in safe_data
        for term in (
            "does not authorise",
            "run a release",
            "create a tag",
            "publish an asset",
            "consumer credentials",
            "publicly",
        )
    ),
    "security authority boundary",
    "a report must not grant release, credential or disclosure authority",
)
check(
    not re.search(r"acknowledg\w*\s+within|within\s+\w+\s+days|timeline\s+will\s+be\s+agreed", security, re.IGNORECASE),
    "security response promises",
    "the policy must not invent an acknowledgement or remediation SLA",
)

readme = read("README.md")
release_status = markdown_section(readme, "Release status", "README release status")
check(
    all(term in release_status for term in ("v0.1.0", "manually created", "notes-only")),
    "README release status",
    "v0.1.0 must be classified as the manually created notes-only marker",
)
check(
    "historical" in release_status and "source" in release_status,
    "README release status",
    "the release must be classified as historical/source-only",
)
check(
    ("no uploaded" in release_status or "zero uploaded" in release_status)
    and "checksummed" in release_status,
    "README release status",
    "the marker must disclose that it has no uploaded checksummed module artefact",
)
check(
    "not a verified distributable module release" in release_status,
    "README release status",
    "the marker must not be described as a verified distributable release",
)
check(
    "is a verified distributable module release" not in release_status,
    "README release status",
    "a compliant sentence must not mask a contradictory distributable-release claim",
)
check(
    all(term in release_status for term in ("silently rewritten", "replaced", "retrofitted")),
    "README release status",
    "the historical marker must not be silently rewritten, replaced or retrofitted",
)
check(
    all(
        term in release_status
        for term in (
            "source-archive path",
            "deterministic",
            "sha256sums",
            "verification instructions",
            "attestations",
            "immutability",
        )
    ),
    "README future self-release",
    "future source releases need design, deterministic assets, checksums, instructions, attestations and immutability",
)
check(
    "where supported" in release_status or "where github supports" in release_status,
    "README future self-release",
    "attestations must be qualified by GitHub support",
)
check(
    "only after" in release_status
    and all(term in release_status for term in ("notes", "assets", "checksums", "attestations", "verified")),
    "README future self-release",
    "immutability must follow verification of the complete release set",
)
check(
    not re.search(r"phase\s+2\s+(?:is\s+)?(?:implemented|available|complete)", release_status),
    "README future self-release",
    "the README must not claim that the future source-archive phase exists",
)

dependabot = read(".github/dependabot.yml")
dependabot_lines = [clean_yaml_line(line) for line in dependabot.splitlines()]
version_lines = [line for line in dependabot_lines if re.fullmatch(r"version:\s*2", line)]
updates_lines = [line for line in dependabot_lines if re.fullmatch(r"updates:\s*", line)]
check(len(version_lines) == 1, "Dependabot version", "configuration must have exactly one version: 2")
check(len(updates_lines) == 1, "Dependabot updates", "configuration must have exactly one updates list")

entry_starts: list[int] = []
for index, line in enumerate(dependabot_lines):
    if re.match(r"^  - package-ecosystem:\s*", line):
        entry_starts.append(index)

entries: list[tuple[str, str, str, list[str]]] = []
for position, start in enumerate(entry_starts):
    end = entry_starts[position + 1] if position + 1 < len(entry_starts) else len(dependabot_lines)
    block = dependabot_lines[start:end]
    ecosystem_match = re.fullmatch(r"  - package-ecosystem:\s*(.+)", block[0])
    directories = [
        scalar(match.group(1))
        for line in block
        if (match := re.fullmatch(r"    directory:\s*(.+)", line))
    ]
    schedule_indices = [
        index for index, line in enumerate(block) if re.fullmatch(r"    schedule:\s*", line)
    ]
    intervals: list[str] = []
    if len(schedule_indices) == 1:
        for line in nested_block(block, schedule_indices[0], 4):
            match = re.fullmatch(r"      interval:\s*(.+)", line)
            if match:
                intervals.append(scalar(match.group(1)))
    if ecosystem_match and len(directories) == 1 and len(intervals) == 1:
        entries.append(
            (scalar(ecosystem_match.group(1)), directories[0], intervals[0], block)
        )
    else:
        failures.append(
            "Dependabot entry structure: each update needs one ecosystem, directory and schedule interval"
        )

actual_tuples = [(ecosystem, directory, interval) for ecosystem, directory, interval, _ in entries]
expected_tuples = [
    ("github-actions", "/", "weekly"),
    ("pip", "/fixtures/demo-pkg", "monthly"),
]
check(
    sorted(actual_tuples) == sorted(expected_tuples),
    "Dependabot roots and cadence",
    f"expected exactly {expected_tuples!r}, found {actual_tuples!r}",
)

github_entries = [entry for entry in entries if entry[:3] == expected_tuples[0]]
if len(github_entries) == 1:
    github_block = github_entries[0][3]
    cooldown_indices = [
        index for index, line in enumerate(github_block) if re.fullmatch(r"    cooldown:\s*", line)
    ]
    cooldown_values: list[str] = []
    if len(cooldown_indices) == 1:
        for line in nested_block(github_block, cooldown_indices[0], 4):
            match = re.fullmatch(r"      default-days:\s*(.+)", line)
            if match:
                cooldown_values.append(scalar(match.group(1)))
    check(
        cooldown_values == ["7"],
        "Dependabot GitHub Actions cooldown",
        "the weekly action entry must retain its seven-day default cooldown",
    )
    github_block_text = "\n".join(github_block)
    check(
        re.search(r"^      codeql-action:\s*$", github_block_text, re.MULTILINE) is not None
        and re.search(
            r'^          - ["\']github/codeql-action\*["\']\s*$',
            github_block_text,
            re.MULTILINE,
        )
        is not None,
        "Dependabot CodeQL group",
        "the CodeQL init and analyse actions must remain grouped",
    )
else:
    check(False, "Dependabot GitHub Actions controls", "weekly root entry is missing or duplicated")


workflow_paths = [
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/release-python.yml",
]
workflows = {path: read(path) for path in workflow_paths}
full_pin = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?@[0-9a-f]{40}$"
)


def workflow_uses(path: str, document: str) -> list[tuple[int, str, list[str]]]:
    lines = document.splitlines()
    parsed: list[tuple[int, str, list[str]]] = []
    uses_key_count = 0
    for index, line in enumerate(lines):
        if re.match(r"^\s*uses\s*:", line):
            uses_key_count += 1
        match = re.fullmatch(r"\s*uses:\s*([^\s#]+)\s*(?:#.*)?", line)
        if not match:
            continue
        target = match.group(1)
        use_indent = len(line) - len(line.lstrip(" "))
        step_indent = max(use_indent - 2, 0)
        start = index
        while start >= 0:
            candidate = lines[start]
            indent = len(candidate) - len(candidate.lstrip(" "))
            if indent == step_indent and candidate.lstrip().startswith("- "):
                break
            start -= 1
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if candidate.strip():
                indent = len(candidate) - len(candidate.lstrip(" "))
                if indent == step_indent and candidate.lstrip().startswith("- "):
                    break
            end += 1
        parsed.append((index, target, lines[max(start, 0) : end]))
    check(
        uses_key_count == len(parsed),
        f"{path} action syntax",
        "every uses key must be an unambiguous scalar action reference",
    )
    return parsed


all_uses: list[tuple[str, int, str, list[str]]] = []
for workflow_path, workflow in workflows.items():
    for line_number, target, step_block in workflow_uses(workflow_path, workflow):
        all_uses.append((workflow_path, line_number, target, step_block))
        check(
            full_pin.fullmatch(target) is not None,
            f"{workflow_path} immutable action pins",
            f"'{target}' is not pinned to one lowercase 40-hex commit",
        )

check(len(all_uses) == 11, "workflow action inventory", "expected exactly 11 external action uses")

checkout_uses = [item for item in all_uses if item[2].startswith("actions/checkout@")]
check(len(checkout_uses) == 4, "checkout inventory", "expected four checkout action steps")
for workflow_path, _, _, step_block in checkout_uses:
    step_text = "\n".join(step_block)
    persist_values = re.findall(
        r"^\s+persist-credentials:\s*([^\s#]+)\s*$", step_text, re.MULTILINE
    )
    check(
        persist_values == ["false"],
        f"{workflow_path} checkout credentials",
        "each checkout step must bind exactly one persist-credentials: false value",
    )


def permission_blocks(document: str) -> list[tuple[int, dict[str, str]]]:
    lines = document.splitlines()
    blocks: list[tuple[int, dict[str, str]]] = []
    permission_keys = 0
    for index, line in enumerate(lines):
        if re.match(r"^\s*permissions\s*:", line):
            permission_keys += 1
        match = re.fullmatch(r"(\s*)permissions:\s*", line)
        if not match:
            continue
        indent = len(match.group(1))
        values: dict[str, str] = {}
        for child in lines[index + 1 :]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            child_indent = len(child) - len(child.lstrip(" "))
            if child_indent <= indent:
                break
            direct = re.fullmatch(rf" {{{indent + 2}}}([a-z-]+):\s*([a-z-]+)(?:\s+#.*)?", child)
            if direct:
                if direct.group(1) in values:
                    failures.append(
                        f"workflow permission key: duplicate '{direct.group(1)}' entry"
                    )
                values[direct.group(1)] = direct.group(2)
        blocks.append((indent, values))
    check(
        permission_keys == len(blocks),
        "workflow permission syntax",
        "permissions must use explicit indentation-bound maps",
    )
    return blocks


expected_permissions = {
    ".github/workflows/ci.yml": [(0, {"contents": "read"})],
    ".github/workflows/codeql.yml": [
        (0, {"contents": "read"}),
        (4, {"contents": "read", "security-events": "write"}),
    ],
    ".github/workflows/release-python.yml": [
        (0, {"contents": "read"}),
        (4, {"attestations": "write", "contents": "write", "id-token": "write"}),
    ],
}
for workflow_path, expected in expected_permissions.items():
    actual = permission_blocks(workflows[workflow_path])
    check(
        actual == expected,
        f"{workflow_path} least privilege",
        f"expected permission maps {expected!r}, found {actual!r}",
    )

codeql = workflows[".github/workflows/codeql.yml"].casefold()
check(
    "language: [python, actions]" in codeql
    and "build-mode: none" in codeql
    and "cancel-in-progress: true" in codeql,
    "CodeQL coverage and concurrency",
    "Python and Actions analysis, build-mode none and cancellation must be retained",
)
check(
    'cron: "5 6 * * 1"' in codeql and "workflow_dispatch:" in codeql,
    "CodeQL schedule",
    "weekly and manual CodeQL triggers must be retained",
)

ci_lines = workflows[".github/workflows/ci.yml"].splitlines()
baseline_invocations = [
    index
    for index, line in enumerate(ci_lines)
    if re.fullmatch(r"\s*run:\s*bash tests/test_repository_baseline\.sh\s*", line)
]
python_setup = [
    index
    for index, line in enumerate(ci_lines)
    if re.search(r"uses:\s*actions/setup-python@[0-9a-f]{40}", line)
]
gate_invocations = [
    index
    for index, line in enumerate(ci_lines)
    if re.fullmatch(r"\s*run:\s*bash tests/test_gates\.sh\s*", line)
]
check(
    len(baseline_invocations) == 1,
    "CI baseline invocation",
    "CI must execute the repository baseline exactly once",
)
check(
    len(baseline_invocations) == 1
    and len(python_setup) == 1
    and len(gate_invocations) == 1
    and python_setup[0] < baseline_invocations[0] < gate_invocations[0],
    "CI baseline ordering",
    "the baseline must run after Python setup and before gate tests",
)

if failures:
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    print(f"repository baseline failed: {len(failures)} failure(s) across {checks} checks", file=sys.stderr)
    raise SystemExit(1)

print(f"repository baseline passed: {checks} checks")
PY
