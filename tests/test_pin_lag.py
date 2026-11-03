"""Contract of ci.yml's pin-lag job: lag warns, an unmeasured pin fails.

The job's comparison step runs here against a stub GitHub and a stub
pin_families.py, so each verdict it can meet is exercised without the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404: runs the workflow's own step under bash
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
STEP = "      - name: Compare each family's consumer pin with main\n"
KEEP, LAG, MISSING, SILENT = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
POLICY = "ryanduguid/release-policy"
# The pin each family's stand-in consumer declares, unless a case overrides it.
CALLERS = {
    "ryanduguid/au-fpa-pack/.github/workflows/release.yml": "release-python",
    "ryanduguid/australian-accounting-skills/.github/workflows/no-ai-attribution.yml": "attribution-policy",
    "ryanduguid/australian-accounting-skills/.github/workflows/virustotal.yml": "virustotal-release",
}
STUB_FAMILIES = f"""\
import sys
old = sys.argv[1]
if old == "{MISSING}":
    sys.exit("pin_families: revision is not a commit in this checkout: " + old)
families = ["release-python", "attribution", "virustotal-release"]
if old == "{SILENT}":
    families.remove("release-python")
for family in families:
    print(family + (": re-pin (scripts/x.py)" if old == "{LAG}" else ": keep the pin"))
"""
STUB_GH = """\
#!/usr/bin/env bash
# gh api -H <header> repos/<owner>/<repo>/contents/<path>?ref=main
# It drains stdin, as a real client may, so the step must not feed its loop there.
cat > /dev/null
endpoint="${@: -1}"
endpoint="${endpoint#repos/}"
file="$CONSUMERS/${endpoint%%\\?*}"
[ -f "$file" ] || { echo "HTTP 404: Not Found" >&2; exit 1; }
cat "$file"
"""


def step_script() -> str:
    lines = CI.read_text(encoding="utf-8").splitlines(keepends=True)
    start = lines.index(STEP)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line)
    return textwrap.dedent("".join(body))


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("jq"),
                     "POSIX bash and jq required")
class PinLagStepTests(unittest.TestCase):
    def run_step(self, pins: dict[str, str]) -> subprocess.CompletedProcess[str]:
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work)
        (work / "scripts").mkdir()
        (work / "scripts" / "pin_families.py").write_text(STUB_FAMILIES, encoding="utf-8")
        (work / "canaries.json").write_text(json.dumps({"families": [{
            "policy_workflow": ".github/workflows/release-python.yml",
            "repository": "ryanduguid/au-fpa-pack",
            "workflow_path": ".github/workflows/release.yml",
        }]}), encoding="utf-8")
        for caller, workflow in CALLERS.items():
            pin = pins.get(workflow, KEEP)
            if pin:
                path = work / "consumers" / caller.replace("/.github/", "/contents/.github/", 1)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"    uses: {POLICY}/.github/workflows/{workflow}.yml@{pin}\n",
                                encoding="utf-8")
        bin_dir = work / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(STUB_GH, encoding="utf-8")
        (bin_dir / "python").symlink_to(sys.executable)
        (bin_dir / "gh").chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "CONSUMERS": str(work / "consumers"), "GITHUB_REPOSITORY": POLICY,
               "GITHUB_STEP_SUMMARY": str(work / "summary.md")}
        result = subprocess.run(["bash", "-c", step_script()], cwd=work, env=env,  # nosec B603 B607
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        self.summary = (work / "summary.md").read_text(encoding="utf-8")
        return result

    def test_current_pins_report_no_lag(self) -> None:
        result = self.run_step({})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("::", result.stdout)
        self.assertEqual(self.summary.count("keep the pin"), 3)

    def test_lag_warns_and_passes(self) -> None:
        result = self.run_step({"attribution-policy": LAG})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"::warning title=Re-pin owed::attribution "
                      f"(ryanduguid/australian-accounting-skills) pins {LAG}; re-pin (scripts/x.py)",
                      result.stdout)
        self.assertNotIn("::error", result.stdout)

    def test_a_pin_missing_from_the_checkout_fails(self) -> None:
        result = self.run_step({"release-python": MISSING})
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error title=Pin lag not measured::release-python (ryanduguid/au-fpa-pack): "
                      "not measured: pin_families: revision is not a commit", result.stdout)
        # The other families are still reported.
        self.assertEqual(self.summary.count("keep the pin"), 2)

    def test_an_unreadable_consumer_pin_fails(self) -> None:
        result = self.run_step({"virustotal-release": ""})
        self.assertEqual(result.returncode, 1)
        self.assertIn("virustotal-release (ryanduguid/australian-accounting-skills): not measured: "
                      "ryanduguid/australian-accounting-skills/.github/workflows/virustotal.yml has no single "
                      ".github/workflows/virustotal-release.yml pin", result.stdout)

    def test_a_missing_family_verdict_fails(self) -> None:
        result = self.run_step({"release-python": SILENT})
        self.assertEqual(result.returncode, 1)
        self.assertIn("not measured: pin_families.py gave no release-python verdict", result.stdout)


if __name__ == "__main__":
    unittest.main()
