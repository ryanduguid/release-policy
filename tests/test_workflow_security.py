"""Keep the workflow security scan and the Dependabot cooldown in place."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIZMOR_ACTION = re.compile(
    r"^      - uses: zizmorcore/zizmor-action@[0-9a-f]{40} # v\d+\.\d+\.\d+$", re.MULTILINE
)


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def ci_job(name: str) -> str:
    workflow = read(".github/workflows/ci.yml")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert match is not None, name
    return match[1]


class WorkflowSecurityTests(unittest.TestCase):
    def test_zizmor_runs_pinned_and_can_fail_the_required_gate(self) -> None:
        job = ci_job("zizmor")
        self.assertRegex(job, ZIZMOR_ACTION)
        self.assertRegex(job, r"version: \d+\.\d+\.\d+\n")
        self.assertIn("advanced-security: false", job)
        self.assertIn("min-severity: low", job)
        self.assertIn("persist-credentials: false", job)
        self.assertNotIn("write", job.split("steps:")[0])
        needs = re.search(r"needs: \[([^\]]*)\]", ci_job("ci-gates"))
        assert needs is not None
        self.assertIn("zizmor", [item.strip() for item in needs[1].split(",")])

    def test_accepted_findings_are_named_by_file_and_line(self) -> None:
        for line in read(".github/zizmor.yml").splitlines():
            if line.lstrip().startswith("- "):
                self.assertRegex(line.strip(), r"^- [a-z-]+\.yml:\d+$")

    def test_every_dependabot_ecosystem_waits_a_week(self) -> None:
        entries = re.split(r"^  - package-ecosystem: ", read(".github/dependabot.yml"), flags=re.MULTILINE)[1:]
        self.assertEqual(
            sorted(entry.split()[0] for entry in entries), ["cargo", "github-actions", "pip"]
        )
        for entry in entries:
            with self.subTest(ecosystem=entry.split()[0]):
                self.assertIn("    cooldown:\n      default-days: 7\n", entry.replace("\r\n", "\n"))


if __name__ == "__main__":
    unittest.main()
