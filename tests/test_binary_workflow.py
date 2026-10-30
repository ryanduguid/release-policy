"""Contract of the Rust binary release family: release-binary.yml and its publisher.

The workflow is the trust boundary: consumer code runs only in read-only build
jobs, and the one privileged job admits their archives as untrusted input,
never runs Cargo, and publishes a fixed asset set under one signer identity.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404: runs this file's own entry point and the audit script
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_skill_workflows import YamlContractAssertions  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
LINUX = "x86_64-unknown-linux-musl"
WINDOWS = "x86_64-pc-windows-msvc"
AUDIT = ROOT / "scripts" / "audit_binaries.sh"
CARGO_AUDIT = "0.22.2"
INPUTS = ("artifact-stem", "binary", "source-directory", "tag-prefix", "required-checks")
# Every release asset, as publish_binaries.sh uploads it.
ASSETS = (
    "$zip", "$tar", "$sbom",
    "$linux.tar.gz", "$linux.spdx.json",
    "$windows.zip", "$windows.spdx.json",
    "$checksums", "$provenance",
)


class BinaryReleaseWorkflowTests(YamlContractAssertions, unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = (WORKFLOWS / "release-binary.yml").read_text(encoding="utf-8")
        self.publisher = (ROOT / "scripts" / "publish_binaries.sh").read_text(encoding="utf-8")

    def job(self, name: str) -> str:
        return self.mapping_block(self.mapping_block(self.workflow, "jobs", indent=0), name, indent=2)

    def step(self, job: str, name: str) -> str:
        marker = f"      - name: {name}\n"
        self.assertEqual(1, job.count(marker), f"step is missing or repeated: {name}")
        start = job.index(marker)
        end = job.find("\n      - name:", start + len(marker))
        return job[start:] if end < 0 else job[start:end]

    def test_closed_interface_and_jobs(self) -> None:
        self.assertEqual(("name", "on", "permissions", "jobs"), self.mapping_keys(self.workflow, indent=0))
        trigger = self.mapping_block(self.workflow, "on", indent=0)
        self.assertEqual(("workflow_call",), self.mapping_keys(trigger, indent=2))
        call = self.mapping_block(trigger, "workflow_call", indent=2)
        self.assertEqual(("inputs",), self.mapping_keys(call, indent=4))
        inputs = self.mapping_block(call, "inputs", indent=4)
        self.assertEqual(INPUTS, self.mapping_keys(inputs, indent=6))
        self.assertEqual("true", self.mapping_value(
            self.mapping_block(inputs, "required-checks", indent=6), "required", indent=8))
        self.assertEqual({"contents": "read"}, self.permission_map(self.workflow, indent=0))
        jobs = self.mapping_block(self.workflow, "jobs", indent=0)
        self.assertEqual(("checks", "build", "publish"), self.mapping_keys(jobs, indent=2))
        for forbidden in ("secrets:", "secrets.", "pull_request_target", "continue-on-error"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.workflow)

    def test_each_job_holds_only_its_own_scopes(self) -> None:
        self.assertEqual({"contents": "read", "actions": "read"},
                         self.permission_map(self.job("checks"), indent=4))
        self.assertEqual({"contents": "read"}, self.permission_map(self.job("build"), indent=4))
        self.assertEqual({"attestations": "write", "contents": "write", "id-token": "write"},
                         self.permission_map(self.job("publish"), indent=4))
        self.assertEqual("checks", self.mapping_value(self.job("build"), "needs", indent=4))
        self.assertEqual("[checks, build]", self.job("publish").split("    needs: ", 1)[1].split("\n")[0])

    def test_consumer_checks_gate_matches_the_archive_family(self) -> None:
        archive = (WORKFLOWS / "release-archive.yml").read_text(encoding="utf-8")
        archive_jobs = self.mapping_block(archive, "jobs", indent=0)
        self.assertEqual(self.mapping_block(archive_jobs, "checks", indent=2), self.job("checks"))

    def test_build_matrix_is_the_two_fixed_targets(self) -> None:
        build = self.job("build")
        self.assertIn(
            "        include:\n"
            f"          - target: {LINUX}\n"
            "            runner: ubuntu-24.04\n"
            f"          - target: {WINDOWS}\n"
            "            runner: windows-2025\n",
            build,
        )
        auditable = self.step(build, "Install the pinned cargo-auditable")
        self.assertIn("tool: cargo-auditable@0.7.7\n", auditable)
        self.assertIn("fallback: none\n", auditable)
        self.assertIn(f"if: matrix.target == '{LINUX}'",
                      self.step(build, "Install the musl C toolchain"))
        static = self.step(build, "Link the C runtime statically")
        self.assertIn(f"if: matrix.target == '{WINDOWS}'", static)
        self.assertIn("+crt-static", static)
        self.assertEqual(1, self.workflow.count("+crt-static"))
        upload = self.step(build, "Hand the archive and its digest to the publisher")
        self.assertIn(
            "name: binary-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.target }}", upload)
        self.assertIn("overwrite: false", upload)
        self.assertIn("policy/scripts/build_binary.sh", build)
        # The packaged documents must be the tagged blobs the publisher compares.
        exact = self.step(build, "Check out files exactly as committed")
        self.assertIn("git config --global core.autocrlf false", exact)
        self.assertLess(build.index(exact),
                        build.index(self.step(build, "Check out the tagged consumer source")))

    def test_publisher_never_runs_consumer_code_or_uploads_artefacts(self) -> None:
        publish = self.job("publish")
        for forbidden in (
            "cargo ", "rustup", "build_binary.sh", "actions/upload-artifact@",
            "pip install", "unittest discover", "pattern:", "merge-multiple", "./scripts/",
            "gh release upload", "secrets: inherit",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, publish)
        # The one tool the publisher installs is the policy's pinned auditor.
        self.assertEqual(1, publish.count("install-action"))
        self.assertIn("install-action", self.step(publish, "Install the pinned cargo-audit"))
        self.assertIn("group: release-${{ github.repository }}-${{ github.ref }}", publish)
        self.assertIn("cancel-in-progress: false", publish)

    def test_publisher_admits_each_named_artefact_before_using_it(self) -> None:
        publish = self.job("publish")
        receive = self.step(publish, "Validate and admit each target's archive")
        self.assertIn("binary_release.py\" receive", receive)
        self.assertIn('--source "$SOURCE_PATH" --root "$GITHUB_WORKSPACE/consumer"', receive)
        linkage = self.step(publish, "Require each admitted executable's linkage")
        self.assertIn('"$GITHUB_WORKSPACE/policy/scripts/admitted_linkage.sh"', linkage)
        self.assertLess(publish.index(receive), publish.index(linkage))
        self.assertLess(publish.index(linkage), publish.index("Attest release asset provenance"))
        for target, label in ((LINUX, "Linux"), (WINDOWS, "Windows")):
            with self.subTest(target=target):
                download = self.step(publish, f"Download the {label} build artefact")
                self.assertIn(
                    f"name: binary-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-{target}\n",
                    download)
                self.assertIn(f"path: ${{{{ runner.temp }}}}/incoming/{target}\n", download)
                sbom = self.step(publish, f"Generate the {label} binary's SPDX SBOM")
                self.assertIn(f"${{{{ runner.temp }}}}/extracted/{target}/", sbom)
                self.assertIn("syft-version: v1.51.0", sbom)
                self.assertLess(publish.index(download), publish.index(receive))
                self.assertLess(publish.index(receive), publish.index(sbom))
        lists = self.step(publish, "Require each binary SBOM to list the package")
        self.assertLess(publish.index(lists), publish.index("Attest release asset provenance"))

    def assert_pinned_cargo_audit(self, install: str) -> None:
        self.assertRegex(install, r"uses: taiki-e/install-action@[0-9a-f]{40} # v2\.\d+\.\d+\n")
        self.assertIn(f"tool: cargo-audit@{CARGO_AUDIT}\n", install)
        self.assertIn("fallback: none\n", install)

    def test_publisher_refuses_any_advisory_against_the_admitted_executables(self) -> None:
        publish = self.job("publish")
        install = self.step(publish, "Install the pinned cargo-audit")
        self.assert_pinned_cargo_audit(install)
        audit = self.step(publish, "Refuse any RustSec advisory against an admitted executable")
        self.assertIn(
            'bash "$GITHUB_WORKSPACE/policy/scripts/audit_binaries.sh" "$RUNNER_TEMP/advisory-db"\n'
            f'          "$RUNNER_TEMP/extracted/{LINUX}/$BINARY"\n'
            f'          "$RUNNER_TEMP/extracted/{WINDOWS}/$BINARY.exe"\n',
            audit)
        self.assertNotIn("working-directory:", audit)
        order = (
            "Require each admitted executable's linkage",
            "Require each binary SBOM to list the package",
            "Install the pinned cargo-audit",
            "Refuse any RustSec advisory against an admitted executable",
            "Write and verify the exact release asset inventory",
            "Attest release asset provenance",
            "Create, inspect and publish the immutable release",
        )
        positions = [publish.index(self.step(publish, name)) for name in order]
        self.assertEqual(sorted(positions), positions)

    def test_scheduled_ci_audits_the_latest_release_executables(self) -> None:
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        checks = self.mapping_block(self.mapping_block(ci, "jobs", indent=0), "checks", indent=2)
        live = self.step(checks, "Audit live canary pins and successful-run evidence")
        install = self.step(checks, "Install the pinned cargo-audit")
        audit = self.step(checks, "Audit the latest release's executables against RustSec")
        condition = live.split("        if: ", 1)[1].split("        env:", 1)[0]
        self.assertIn("github.event_name == 'schedule'", condition)
        for step in (install, audit):
            with self.subTest(step=step.splitlines()[0]):
                self.assertIn(f"        if: {condition}", step)
        self.assert_pinned_cargo_audit(install)
        self.assertLess(checks.index(live), checks.index(install))
        self.assertLess(checks.index(install), checks.index(audit))
        self.assertIn('gh release download --repo "$GITHUB_REPOSITORY" --dir "$latest"', audit)
        self.assertIn(
            'bash scripts/audit_binaries.sh "$RUNNER_TEMP/advisory-db" \\\n'
            '            "$latest/release-policy" "$latest/release-policy.exe"\n',
            audit)
        self.assertEqual(1, ci.count("audit_binaries.sh"))

    def test_audit_script_fails_closed_without_an_ignore_list(self) -> None:
        script = AUDIT.read_text(encoding="utf-8")
        self.assertIn(
            'work="$(mktemp -d)"\ncd "$work"\n'
            'cargo-audit audit --db "$db" --no-yanked --deny warnings bin "$@" 2>&1 | tee audit.log\n',
            script)
        self.assertIn("set -euo pipefail\n", script)
        for text in (script, self.workflow, (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")):
            for waiver in ("--ignore", "--no-fetch", "--stale", "--file", "--url"):
                with self.subTest(waiver=waiver):
                    self.assertNotIn(waiver, text)

    def test_publisher_gates_attests_and_publishes_the_fixed_assets(self) -> None:
        publish = self.job("publish")
        order = (
            "Require a release-ready tag, repository and archive contract",
            "Require the Cargo package, binary and toolchain to match the release",
            "Build deterministic source archives",
            "Validate and admit each target's archive",
            "Write and verify the exact release asset inventory",
            "Attest release asset provenance",
            "Write the provenance bundle as a release asset",
            "Verify provenance and SBOM attestations before publication",
            "Re-check the remote tag, main and release absence",
            "Create, inspect and publish the immutable release",
        )
        positions = [publish.index(self.step(publish, name)) for name in order]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("run_archive_release_gates", publish)
        self.assertIn('signer="ryanduguid/release-policy/.github/workflows/release-binary.yml"', publish)
        self.assertIn('--signer-digest "$MODULE_SHA"', publish)
        self.assertIn('--bundle "$base.intoto.jsonl"', publish)
        inventory = self.step(publish, "Write and verify the exact release asset inventory")
        self.assertNotIn("intoto", inventory)
        provenance = self.step(publish, "Attest release asset provenance")
        self.assertEqual(8, len(re.findall(r"^            \$\{\{", provenance, re.MULTILINE)))
        self.assertEqual(3, publish.count("sbom-path:"))
        self.assertIn('bash "$GITHUB_WORKSPACE/policy/scripts/publish_binaries.sh"', publish)

    def test_publication_script_uploads_exactly_the_inventory(self) -> None:
        self.assertIn("assets=(\n", self.publisher)
        listed = self.publisher.split("assets=(\n", 1)[1].split("\n)\n", 1)[0].split()
        self.assertEqual(sorted(ASSETS), sorted(name.strip('"') for name in listed))
        uploads = re.findall(r'^upload_asset "(\$[a-z]+(?:\.[a-z.]+)?)" ', self.publisher, re.MULTILINE)
        self.assertEqual(list(ASSETS), uploads)
        self.assertIn('. "$(dirname "$0")/publish_common.sh"', self.publisher)
        self.assertIn("finalise_release", self.publisher)

    def test_direct_test_entry_point_loads(self) -> None:
        result = subprocess.run(  # nosec B603  # nosemgrep
            [sys.executable, "-B", str(Path(__file__).resolve()), "--help"],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("usage:", result.stdout)


# Stands in for cargo-audit: records where and how it ran, then behaves as the
# real tool does for the case under test. As in the real tool, an advisory is
# reported after every binary's data was found.
FAKE_CARGO_AUDIT = """#!/usr/bin/env bash
{ printf '%s\\n' "$@"; printf -- '--listing\\n'; ls -A; } > "$FAKE_LOG"
mkdir -p "$3"
if [ "$FAKE_CASE" = fetch-fails ]; then
  echo "error: couldn't fetch advisory database" >&2
  exit 1
fi
shift 7
for binary in "$@"; do
  if [ "$FAKE_CASE" != no-data ] || [ "$binary" != "${!#}" ]; then
    echo "       Found 'cargo auditable' data in $binary (2 dependencies)" >&2
  fi
done
if [ "$FAKE_CASE" = advisory ]; then
  echo "error: 1 vulnerability found" >&2
  exit 1
fi
"""


@unittest.skipUnless(shutil.which("bash"), "bash required")
class AuditScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake = fake_bin / "cargo-audit"
        fake.write_text(FAKE_CARGO_AUDIT, encoding="utf-8", newline="\n")
        fake.chmod(0o755)
        # The caller's directory holds a project audit config the script must not read.
        self.caller = self.root / "caller"
        (self.caller / ".cargo").mkdir(parents=True)
        (self.caller / ".cargo" / "audit.toml").write_text(
            "[advisories]\nignore = []\n", encoding="utf-8")
        self.cargo_home = self.root / "cargo-home"
        self.cargo_home.mkdir()
        self.log = self.root / "calls"
        self.db = str(self.root / "advisory-db")
        self.binaries = [str(self.root / "tool"), str(self.root / "tool.exe")]
        self.env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CARGO_HOME": str(self.cargo_home),
            "FAKE_LOG": str(self.log),
        }

    def audit(self, case: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # nosec B603 B607  # nosemgrep
            ["bash", str(AUDIT), *arguments], cwd=self.caller, env={**self.env, "FAKE_CASE": case},
            check=False, capture_output=True, text=True,
        )

    def test_clean_audit_of_every_binary_passes_from_an_empty_directory(self) -> None:
        result = self.audit("clean", self.db, *self.binaries)
        self.assertEqual(0, result.returncode, result.stderr)
        arguments, listing = self.log.read_text(encoding="utf-8").split("--listing\n")
        self.assertEqual(
            ["audit", "--db", self.db, "--no-yanked", "--deny", "warnings", "bin", *self.binaries],
            arguments.splitlines())
        self.assertLessEqual(set(listing.split()), {"audit.log"})

    def test_each_failure_fails_closed(self) -> None:
        for case, message in (
            ("advisory", "1 vulnerability found"),
            ("fetch-fails", "couldn't fetch advisory database"),
            ("no-data", f"read no embedded dependency list from {self.binaries[-1]}"),
        ):
            with self.subTest(case=case):
                shutil.rmtree(self.db, ignore_errors=True)
                result = self.audit(case, self.db, *self.binaries)
                self.assertEqual(1, result.returncode)
                self.assertIn(message, result.stdout + result.stderr)

    def test_refuses_before_auditing(self) -> None:
        self.assertEqual(64, self.audit("clean", self.db).returncode)
        Path(self.db).mkdir()
        self.assertIn("must be fetched fresh", self.audit("clean", self.db, *self.binaries).stderr)
        Path(self.db).rmdir()
        (self.cargo_home / "audit.toml").write_text("", encoding="utf-8")
        refused = self.audit("clean", self.db, *self.binaries)
        self.assertEqual(1, refused.returncode)
        self.assertIn("refusing to audit with", refused.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
