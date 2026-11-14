# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""The callers `release-policy init` writes pass the policy's own caller checks.

tests/rust/init.rs requires init to write exactly each golden in
tests/data/init. This runs on those goldens what the reusable workflow runs on
a caller: the pin must be the commit of the release it names, every input must
be one that release's workflow declares, with each required input present, and
the component inputs, metadata and required-check list must pass the same
gate functions and parsers the release jobs call. CI lints the goldens with
actionlint.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404: runs git and the policy's own gate functions
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import required_checks  # noqa: E402

SHAPES = ROOT / "tests" / "data" / "init"
USES = re.compile(
    r"^    uses: ryanduguid/release-policy/(?P<workflow>\.github/workflows/release-[a-z]+\.yml)"
    r"@(?P<sha>[0-9a-f]{40}) # (?P<tag>v[0-9]+\.[0-9]+\.[0-9]+)$",
    re.MULTILINE,
)
# What each family's release jobs run on the caller's inputs, from the policy
# checkout, in the consumer's root: the component selectors first, then the
# family's metadata. Each prints the stem and version the release would use.
GATES = {
    "release-python.yml": 'derive_name_version "$VERSION_PARSER" "$VERSION_FILE" .',
    "release-archive.yml": 'derive_archive_metadata "$ARTIFACT_STEM" VERSION .',
    "release-binary.yml": (
        '{ read -r stem; read -r version; } < <(derive_archive_metadata "$ARTIFACT_STEM" VERSION .)\n'
        'test -n "$version"\n'
        '"$PYTHON" "$POLICY/binary_release.py" cargo-package --manifest Cargo.toml'
        ' --version "$version" --binary "${BINARY:-$stem}" >/dev/null\n'
        'printf "%s\\n%s\\n" "$stem" "$version"'
    ),
}
EXPECTED = {
    "python": ("release-python.yml", "example_pkg\n1.2.3\n"),
    "python-literal": ("release-python.yml", "example_pkg\n0.4.1\n"),
    "binary": ("release-binary.yml", "example-tool\n0.1.0\n"),
    "archive": ("release-archive.yml", "example-archive\n2.0.0\n"),
}


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(  # nosec B603 B607: fixed git arguments
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def caller_inputs(text: str) -> dict[str, str]:
    """The `with:` mapping of the caller's one job: plain values and `|` blocks."""
    inputs: dict[str, str] = {}
    block = None
    for line in text.split("    with:\n", 1)[1].splitlines():
        if block is not None and line.startswith("        "):
            inputs[block] += line[8:] + "\n"
            continue
        key, value = line.strip().split(": ", 1)
        block = key if value == "|" else None
        inputs[key] = "" if block else value
    return inputs


def declared_inputs(workflow: str) -> dict[str, bool]:
    """Each `workflow_call` input of a reusable workflow, and whether it is required."""
    block = workflow.split("\n  workflow_call:\n    inputs:\n", 1)[1]
    declared: dict[str, bool] = {}
    name = ""
    for line in block.splitlines():
        if line.strip() and not line.startswith("      "):
            break
        if re.fullmatch(r"      [a-z-]+:", line):
            name = line.strip()[:-1]
            declared[name] = False
        elif line == "        required: true":
            declared[name] = True
    return declared


class InitGoldenTests(unittest.TestCase):
    def test_every_shape_has_a_golden_and_an_expectation(self) -> None:
        self.assertEqual(sorted(p.name for p in SHAPES.iterdir()), sorted(EXPECTED))

    def test_each_caller_pins_the_commit_of_the_release_it_names(self) -> None:
        for name, (workflow, _) in EXPECTED.items():
            with self.subTest(name):
                uses = USES.search((SHAPES / name / "release.yml").read_text(encoding="utf-8"))
                assert uses is not None
                self.assertEqual(uses["workflow"], f".github/workflows/{workflow}")
                tag = git("rev-parse", "--verify", f"refs/tags/{uses['tag']}^{{commit}}")
                self.assertEqual(tag.strip(), uses["sha"])

    def test_each_caller_names_only_inputs_its_pinned_workflow_declares(self) -> None:
        for name in EXPECTED:
            with self.subTest(name):
                text = (SHAPES / name / "release.yml").read_text(encoding="utf-8")
                uses = USES.search(text)
                assert uses is not None
                declared = declared_inputs(git("show", f"{uses['sha']}:{uses['workflow']}"))
                given = caller_inputs(text)
                self.assertLessEqual(set(given), set(declared))
                self.assertLessEqual({k for k, required in declared.items() if required}, set(given))

    def test_each_caller_passes_the_gates_its_release_runs(self) -> None:
        for name, (workflow, expected) in EXPECTED.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                consumer = Path(temporary) / "consumer"
                shutil.copytree(SHAPES / name / "input", consumer)
                git("init", "-q", cwd=consumer)
                git("add", "-A", cwd=consumer)
                given = caller_inputs((SHAPES / name / "release.yml").read_text(encoding="utf-8"))
                required_checks.parse_required_checks(given["required-checks"])
                env = {
                    **os.environ,
                    "PYTHON": sys.executable,
                    "POLICY": str(ROOT / "scripts"),
                    "SOURCE_DIRECTORY": given.get("source-directory", "."),
                    "TAG_PREFIX": given.get("tag-prefix", ""),
                    "VERSION_PARSER": given.get("version-parser", "pyproject"),
                    "VERSION_FILE": given.get("version-file", "pyproject.toml"),
                    "ARTIFACT_STEM": given.get("artifact-stem", ""),
                    "BINARY": given.get("binary", ""),
                }
                script = (
                    '. "$POLICY/gates.sh"\n'
                    'validate_release_inputs "$SOURCE_DIRECTORY" "$TAG_PREFIX" >/dev/null\n'
                    + GATES[workflow]
                )
                result = subprocess.run(  # nosec B603 B607: the policy's own gate functions
                    ["bash", "-c", script], cwd=consumer, env=env, capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.replace("\r\n", "\n"), expected)


if __name__ == "__main__":
    unittest.main()
