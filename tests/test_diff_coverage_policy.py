"""Repository contract for the risk-based changed-line coverage pilot."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DiffCoveragePolicyTests(unittest.TestCase):
    def test_python_policy_scripts_are_covered_by_one_scoped_diff_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn('"coverage==7.15.4"', workflow)
        self.assertIn('"diff-cover==10.5.1"', workflow)
        self.assertIn("coverage run --branch --source=scripts", workflow)
        self.assertIn('--include="scripts/*.py"', workflow)
        self.assertIn("--compare-branch=origin/main", workflow)
        self.assertIn("--branch-coverage", workflow)
        self.assertIn("--fail-under=100", workflow)
        self.assertEqual(
            workflow.count('-m unittest discover -s tests -p "test_*.py" -v'),
            1,
        )
        self.assertIn(".coverage\n", ignore.replace("\r\n", "\n"))
        self.assertIn("coverage.xml\n", ignore.replace("\r\n", "\n"))


if __name__ == "__main__":
    unittest.main()
