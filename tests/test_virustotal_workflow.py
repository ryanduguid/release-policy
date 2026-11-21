# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: Apache-2.0

"""Exercise the release scan's Bash steps with synthetic files and offline service stubs."""

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404: exercises the workflow steps using offline stubs
import tempfile
import textwrap
import unittest
from itertools import takewhile
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/virustotal-release.yml"
DIRECT = "https://www.virustotal.com/api/v3/files"
LARGE = "https://www.virustotal.com/_ah/upload/fixture"
MIB = 1024 * 1024


def step(name: str) -> str:
    source = WORKFLOW.read_text(encoding="utf-8").split(f"      - name: {name}\n", 1)[1]
    lines = source.split("        run: |\n", 1)[1].splitlines(keepends=True)
    return textwrap.dedent("".join(takewhile(lambda line: line.startswith("          ")
                                             or not line.strip(), lines)))


STUBS = r'''
# The workflow runs on Linux. Use LF output when Git Bash finds a native Windows jq.
if [[ $OSTYPE == msys* || $OSTYPE == cygwin* ]]; then
  jq() { MSYS2_ARG_CONV_EXCL='*' command jq --binary "$@"; }
fi
sleep() { echo "sleep $1" >> calls; }
gh() {
  case "$*" in
    *--jq*) printf '%s\n' "$VISIBILITY" ;;
    *releases/tags/*) cat fixture.json ;;
    *"release download"*) echo download >> calls; [[ -f payload ]] && cp payload assets/fixture ;;
    *"-X PATCH"*) echo annotate >> calls ;;
    *) return 1 ;;
  esac
}
curl() {
  local url= output= config=
  while (( $# )); do
    case "$1" in
      --output|-o) output=$2; shift 2 ;;
      --config)
        config=$2
        if [[ $config == *vt-upload.curl ]]; then
          url=$(sed -n 's/^url = "\(.*\)"$/\1/p' "$config")
        fi
        shift 2 ;;
      https://*) url=$1; shift ;;
      *) shift ;;
    esac
  done
  printf '%s\n' "$url" >> calls
  case "$url" in
    */upload_url) printf '%s' "$LOOKUP" > "$output" ;;
    *) [[ $REFUSE != yes ]] || return 22; printf '%s' "$ANALYSIS" > "$output" ;;
  esac
}
'''


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "requires Bash and jq")
class VirusTotalWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "calls").touch()

    def run_step(self, name: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "RUNNER_TEMP": ".", "GITHUB_REPOSITORY": "example/fixture",
               "TAG": "v1", "VT_API_KEY": "a" * 64, "VISIBILITY": "public", "REFUSE": "no",
               "GITHUB_OUTPUT": "step-output",
               "LOOKUP": json.dumps({"data": LARGE}),
               "ANALYSIS": json.dumps({"data": {"id": "fixture", "type": "analysis"}}), **overrides}
        return subprocess.run(["bash", "-c", STUBS + step(name)], env=env, cwd=self.root,
                              capture_output=True, text=True, timeout=15, check=False)  # nosec B603

    def calls(self) -> list[str]:
        return (self.root / "calls").read_text(encoding="utf-8").splitlines()

    def asset(self, name: str, size: int) -> None:
        (self.root / "assets").mkdir(exist_ok=True)
        with (self.root / "assets" / name).open("wb") as output:
            output.truncate(size)

    def test_boundary_and_large_file_share_one_request_budget(self) -> None:
        self.asset("a", 32 * MIB)
        self.asset("b", 32 * MIB + 1)
        result = self.run_step("Upload each asset to VirusTotal",
                               LOOKUP=json.dumps({"data": LARGE.replace("https:", "http:")}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [DIRECT, "sleep 16", DIRECT + "/upload_url", "sleep 16", LARGE])
        self.assertFalse(list(self.root.glob("vt.*")))
        self.assertFalse((self.root / "vt-upload.curl").exists())

    def test_unexpected_upload_destination_stops_before_submission(self) -> None:
        self.asset("a", 650_000_000)
        result = self.run_step("Upload each asset to VirusTotal",
                               LOOKUP=json.dumps({"data": "https://example.invalid/upload"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [DIRECT + "/upload_url"])

    def test_refused_upload_or_missing_analysis_id_fails(self) -> None:
        self.asset("a", 2)
        for response in ({"REFUSE": "yes"}, {"ANALYSIS": '{"data":{"type":"analysis","id":""}}'}):
            with self.subTest(response=response):
                result = self.run_step("Upload each asset to VirusTotal", **response)
                self.assertNotEqual(result.returncode, 0)

    def test_ineligible_release_stops_before_download(self) -> None:
        name = "Download the release assets and check their digests"
        for visibility, draft, size in (("private", False, 1), ("internal", False, 1),
                                        ("public", True, 1), ("public", False, 650_000_001)):
            with self.subTest(visibility=visibility, draft=draft, size=size):
                (self.root / "calls").write_text("", encoding="utf-8")
                fixture = {"draft": draft, "assets": [{"name": "fixture", "size": size,
                                                       "digest": "sha256:" + "a" * 64}]}
                (self.root / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
                result = self.run_step(name, VISIBILITY=visibility)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_maximum_size_reaches_download(self) -> None:
        fixture = {"draft": False, "assets": [{"name": "fixture", "size": 650_000_000,
                                               "digest": "sha256:" + "a" * 64}]}
        (self.root / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
        self.run_step("Download the release assets and check their digests")
        self.assertEqual(self.calls(), ["download"])

    def test_notes_link_only_the_release_assets_that_were_scanned(self) -> None:
        payload = b"fixture"
        digest = hashlib.sha256(payload).hexdigest()
        (self.root / "payload").write_bytes(payload)
        asset = {"name": "fixture", "size": len(payload), "digest": "sha256:" + digest}
        prior = "Release notes\n<!-- virustotal:start -->\nold report\n<!-- virustotal:end -->\n"
        fixture = {"id": 1, "draft": False, "body": prior, "assets": [asset]}
        manifest = self.root / "fixture.json"
        manifest.write_text(json.dumps(fixture), encoding="utf-8")
        prepared = self.run_step("Download the release assets and check their digests")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        signature = (self.root / "step-output").read_text().strip().removeprefix("manifest=")
        result = self.run_step("Replace the VirusTotal section of the release notes",
                               SCANNED_MANIFEST=signature)
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = (self.root / "notes.md").read_text()
        self.assertIn("https://www.virustotal.com/gui/file/" + digest, notes)
        self.assertEqual(notes.count("<!-- virustotal:start -->"), 1)
        self.assertNotIn("old report", notes)
        self.assertIn("Release notes", notes)
        asset["name"] = "replacement"
        manifest.write_text(json.dumps(fixture), encoding="utf-8")
        result = self.run_step("Replace the VirusTotal section of the release notes",
                               SCANNED_MANIFEST=signature)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["download", "annotate"])
