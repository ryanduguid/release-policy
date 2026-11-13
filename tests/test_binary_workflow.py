# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""Contract of the Rust binary release family: release-binary.yml and its publisher.

The workflow is the trust boundary: consumer code runs only in read-only build
jobs and fetched third-party tools only in the read-only admit job. The one
privileged job admits the build archives again as untrusted input, checks the
admit job's hand-off against its own and never runs Cargo or those tools. It
publishes a fixed asset set under one signer identity.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404: runs this file's own entry point and the audit scripts
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tests.test_skill_workflows import YamlContractAssertions  # noqa: E402

import binary_release  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
LINUX = "x86_64-unknown-linux-musl"
WINDOWS = "x86_64-pc-windows-msvc"
# Each target, the runner that builds it natively and its label in step names.
TARGETS = (
    (LINUX, "ubuntu-24.04", "Linux"),
    ("aarch64-unknown-linux-musl", "ubuntu-24.04-arm", "Linux arm64"),
    ("aarch64-apple-darwin", "macos-15", "macOS arm64"),
    (WINDOWS, "windows-2025", "Windows"),
)
AUDIT = ROOT / "scripts" / "audit_binaries.sh"
CANARY_AUDIT = ROOT / "scripts" / "audit_binary_canaries.sh"
CARGO_AUDIT = "0.22.2"
INPUTS = ("artifact-stem", "binary", "data-files", "source-directory", "tag-prefix",
          "required-checks")
# Every release asset, as publish_binaries.sh uploads it.
ASSETS = (
    "$zip", "$tar", "$sbom",
    "$linux.tar.gz", "$linux.spdx.json",
    "$linux_arm64.tar.gz", "$linux_arm64.spdx.json",
    "$macos_arm64.tar.gz", "$macos_arm64.spdx.json",
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
        self.assertEqual(("checks", "build", "admit", "publish"), self.mapping_keys(jobs, indent=2))
        for forbidden in ("secrets:", "secrets.", "pull_request_target", "continue-on-error"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.workflow)

    def test_each_job_holds_only_its_own_scopes(self) -> None:
        self.assertEqual({"contents": "read", "actions": "read"},
                         self.permission_map(self.job("checks"), indent=4))
        self.assertEqual({"contents": "read"}, self.permission_map(self.job("build"), indent=4))
        self.assertEqual({"contents": "read"}, self.permission_map(self.job("admit"), indent=4))
        self.assertEqual({"attestations": "write", "contents": "write", "id-token": "write"},
                         self.permission_map(self.job("publish"), indent=4))
        self.assertEqual("checks", self.mapping_value(self.job("build"), "needs", indent=4))
        for name, needs in (("admit", "[checks, build]"), ("publish", "[checks, build, admit]")):
            with self.subTest(job=name):
                job = self.job(name)
                self.assertEqual(needs, job.split("    needs: ", 1)[1].split("\n")[0])
                self.assertEqual("ubuntu-24.04", self.mapping_value(job, "runs-on", indent=4))
                self.assertEqual("20", self.mapping_value(job, "timeout-minutes", indent=4))
        self.assertNotIn("concurrency:", self.job("admit"))

    def test_consumer_checks_gate_matches_the_archive_family(self) -> None:
        archive = (WORKFLOWS / "release-archive.yml").read_text(encoding="utf-8")
        archive_jobs = self.mapping_block(archive, "jobs", indent=0)
        self.assertEqual(self.mapping_block(archive_jobs, "checks", indent=2), self.job("checks"))

    def test_build_matrix_is_the_fixed_targets_each_on_its_own_runner(self) -> None:
        build = self.job("build")
        self.assertEqual(tuple(binary_release.TARGETS), tuple(target for target, _, _ in TARGETS))
        self.assertIn(
            "        include:\n" + "".join(
                f"          - target: {target}\n            runner: {runner}\n"
                for target, runner, _ in TARGETS),
            build,
        )
        # The admit and publish jobs loop over the same targets.
        targets = " ".join(binary_release.TARGETS)
        for name in ("admit", "publish"):
            with self.subTest(job=name):
                self.assertEqual(targets, self.mapping_value(
                    self.mapping_block(self.job(name), "env", indent=4), "TARGETS", indent=6))
        auditable = self.step(build, "Install the pinned cargo-auditable")
        self.assertIn("tool: cargo-auditable@0.7.7\n", auditable)
        self.assertIn("fallback: none\n", auditable)
        self.assertIn("if: endsWith(matrix.target, '-linux-musl')",
                      self.step(build, "Install the musl C toolchain"))
        static = self.step(build, "Link the C runtime statically")
        self.assertIn(f"if: matrix.target == '{WINDOWS}'", static)
        self.assertIn("+crt-static", static)
        self.assertEqual(1, self.workflow.count("+crt-static"))
        upload = self.step(build, "Hand the archive and its digest to the publisher")
        self.assertIn(
            "name: binary-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.target }}", upload)
        self.assertIn("overwrite: false", upload)
        script = self.step(build, "Test, build, package and check the executables")
        self.assertIn("policy/scripts/build_binary.sh", script)
        self.assertIn("DATA_FILES: ${{ inputs.data-files }}\n", script)
        self.assertIn('"$EPOCH" "$DATA_FILES"', script)
        self.assertIn("RELEASE_SOURCE_COMMIT: ${{ github.sha }}\n", script)
        # The packaged documents must be the tagged blobs the publisher compares.
        exact = self.step(build, "Check out files exactly as committed")
        self.assertIn("git config --global core.autocrlf false", exact)
        self.assertLess(build.index(exact),
                        build.index(self.step(build, "Check out the tagged consumer source")))

    @unittest.skipUnless(shutil.which("bash"), "bash required")
    def test_the_build_exports_only_a_full_release_source_commit(self) -> None:
        step = self.step(self.job("build"), "Test, build, package and check the executables")
        script = re.search(r"(?s)\n        run: \|\n((?:          .*\n)+)", step)
        assert script is not None
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = workspace / "policy" / "scripts" / "build_binary.sh"
        stub.parent.mkdir(parents=True)
        stub.write_text('printf %s "$RELEASE_SOURCE_COMMIT" > "$GITHUB_WORKSPACE/seen"\n',
                        encoding="utf-8")
        for commit, built in (("a" * 40, True), ("", False), ("a" * 39, False), ("A" * 40, False)):
            with self.subTest(commit=commit):
                (workspace / "seen").unlink(missing_ok=True)
                result = subprocess.run(  # nosec B603 B607  # nosemgrep
                    ["bash", "-c", re.sub(r"(?m)^ {10}", "", script.group(1))],
                    env={**os.environ, **dict.fromkeys(
                        ("TARGET", "STEM", "VERSION", "BINARY", "SOURCE_PATH", "RUNNER_TEMP",
                         "EPOCH", "DATA_FILES"), "x"),
                         "GITHUB_WORKSPACE": str(workspace), "RELEASE_SOURCE_COMMIT": commit},
                    check=False, capture_output=True, text=True)
                self.assertEqual(built, result.returncode == 0, result.stderr)
                seen = workspace / "seen"
                self.assertEqual(commit if built else None,
                                 seen.read_text(encoding="utf-8") if seen.exists() else None)

    def assert_in_order(self, job: str, *names: str) -> None:
        positions = [job.index(self.step(job, name)) for name in names]
        self.assertEqual(sorted(positions), positions)

    def test_publisher_runs_no_consumer_code_or_third_party_tool_and_uploads_nothing(self) -> None:
        publish = self.job("publish")
        for forbidden in (
            "cargo ", "rustup", "build_binary.sh", "actions/upload-artifact@",
            "pip install", "unittest discover", "pattern:", "merge-multiple", "./scripts/",
            "gh release upload", "secrets: inherit",
            # The admit job's fetched tools and the policy scripts that run them.
            "anchore/", "syft", "install-action", "cargo-audit", "audit_binaries.sh",
            "admitted_linkage.sh", "llvm-readobj",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, publish)
        self.assertEqual(
            {"actions/checkout", "actions/setup-python", "actions/download-artifact",
             "actions/attest"},
            set(re.findall(r"uses: ([^@\s]+)@", publish)))
        self.assertIn("group: release-${{ github.repository }}-${{ github.ref }}", publish)
        self.assertIn("cancel-in-progress: false", publish)

    def test_admit_job_holds_no_token_and_runs_no_consumer_code(self) -> None:
        admit = self.job("admit")
        for forbidden in (
            "cargo ", "build_binary.sh", "pip install", "unittest discover", "pattern:",
            "merge-multiple", "./scripts/", "secrets: inherit", "GH_TOKEN", "github.token",
            "actions/attest@", "publish_binaries.sh",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, admit)
        # The same checkouts as the publisher's: the tag with history, and the policy pin.
        for step in ("Check out the tagged consumer source",
                     "Check out release-policy at the calling pin", "Require an immutable policy pin"):
            with self.subTest(step=step):
                self.assertEqual(self.step(self.job("publish"), step), self.step(admit, step))

    def test_admit_and_publish_each_admit_the_named_build_artefacts(self) -> None:
        for job in (self.job("admit"), self.job("publish")):
            receive = self.step(job, "Validate and admit each target's archive")
            self.assertIn("binary_release.py\" receive", receive)
            self.assertIn('--source "$SOURCE_PATH" --root "$GITHUB_WORKSPACE/consumer"', receive)
            self.assertIn("for target in $TARGETS; do", receive)
            self.assertIn('--data-files "$DATA_FILES"', receive)
            self.assertIn("DATA_FILES: ${{ inputs.data-files }}\n", receive)
            sources = self.step(job, "Build deterministic source archives")
            self.assertIn("policy/scripts/build_release_archives.py", sources)
            for target, _, label in TARGETS:
                with self.subTest(target=target):
                    download = self.step(job, f"Download the {label} build artefact")
                    self.assertIn(
                        f"name: binary-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-{target}\n",
                        download)
                    self.assertIn(f"path: ${{{{ runner.temp }}}}/incoming/{target}\n", download)
                    self.assertLess(job.index(download), job.index(receive))

    def test_admit_checks_describes_and_audits_before_the_hand_off(self) -> None:
        admit = self.job("admit")
        linkage = self.step(admit, "Require each admitted executable's linkage")
        self.assertIn('"$GITHUB_WORKSPACE/policy/scripts/admitted_linkage.sh"', linkage)
        self.assertIn('"$RUNNER_TEMP/extracted" "$BINARY" "${targets[@]}"', linkage)
        stage = self.step(admit, "Stage each target's executables for its SBOM")
        self.assertIn('cp "$RUNNER_TEMP/extracted/$target/$name$suffix" "$RUNNER_TEMP/sbom/$target/"\n',
                      stage)
        self.assertIn("syft-version: v1.51.0",
                      self.step(admit, "Generate an SPDX SBOM for the source ZIP archive"))
        for target, _, label in TARGETS:
            with self.subTest(target=target):
                sbom = self.step(admit, f"Generate the {label} binary's SPDX SBOM")
                # The target's executables alone: a data file such as a lock file
                # in the admitted directory would add packages it does not ship.
                self.assertIn(f"path: ${{{{ runner.temp }}}}/sbom/{target}\n", sbom)
                self.assertIn(f"-{target}.spdx.json\n", sbom)
                self.assertIn("syft-version: v1.51.0", sbom)
        self.assert_in_order(
            admit,
            "Build deterministic source archives",
            "Generate an SPDX SBOM for the source ZIP archive",
            "Validate and admit each target's archive",
            "Require each admitted executable's linkage",
            "Stage each target's executables for its SBOM",
            *(f"Generate the {label} binary's SPDX SBOM" for _, _, label in TARGETS),
            "Require each binary SBOM to list the package",
            "Install the pinned cargo-audit",
            "Refuse any RustSec advisory against an admitted executable",
            "Record the SHA-256 of each file for the publisher",
            "Hand the admitted archives and SBOMs to the publisher",
        )
        upload = self.step(admit, "Hand the admitted archives and SBOMs to the publisher")
        name = "name: admitted-${{ github.run_id }}-${{ github.run_attempt }}\n"
        self.assertIn(name, upload)
        self.assertIn("overwrite: false", upload)
        self.assertIn(name, self.step(self.job("publish"), "Download the admit job's archives and SBOMs"))

    def test_each_handed_over_file_has_one_digest_output_that_publish_checks(self) -> None:
        admit, publish = self.job("admit"), self.job("publish")
        outputs = self.mapping_block(admit, "outputs", indent=4)
        roles = tuple(binary_release.HANDOFF)
        self.assertEqual(roles, self.mapping_keys(outputs, indent=6))
        record = self.step(admit, "Record the SHA-256 of each file for the publisher")
        self.assertIn("id: digests\n", record)
        self.assertIn('binary_release.py" digests\n', record)
        self.assertIn('>> "$GITHUB_OUTPUT"', record)
        accept = self.step(publish, "Accept the admit job's SBOMs once its archives match this job's")
        self.assertIn('binary_release.py" accept\n', accept)
        self.assertIn('--incoming "$RUNNER_TEMP/admitted" --dist "$SOURCE_PATH/dist"', accept)
        self.assertEqual(len(roles), accept.count("--digest "))
        # The digests reach no other publish step: they carry no authority.
        self.assertEqual(len(roles), publish.count("needs.admit."))
        self.assertEqual(len(roles), accept.count("needs.admit."))
        for role in roles:
            variable = role.upper().replace("-", "_")
            with self.subTest(role=role):
                self.assertEqual(f"${{{{ steps.digests.outputs.{role} }}}}",
                                 self.mapping_value(outputs, role, indent=6))
                self.assertIn(f"{variable}: ${{{{ needs.admit.outputs.{role} }}}}\n", accept)
                self.assertIn(f'--digest "{role}=${variable}"', accept)

    def assert_pinned_cargo_audit(self, install: str) -> None:
        self.assertRegex(install, r"uses: taiki-e/install-action@[0-9a-f]{40} # v2\.\d+\.\d+\n")
        self.assertIn(f"tool: cargo-audit@{CARGO_AUDIT}\n", install)
        self.assertIn("fallback: none\n", install)

    def test_admit_refuses_any_advisory_against_the_admitted_executables(self) -> None:
        admit = self.job("admit")
        install = self.step(admit, "Install the pinned cargo-audit")
        self.assert_pinned_cargo_audit(install)
        audit = self.step(admit, "Refuse any RustSec advisory against an admitted executable")
        self.assertIn(
            '          for target in $TARGETS; do\n'
            '            suffix=""\n'
            '            [[ "$target" != *-windows-msvc ]] || suffix=.exe\n'
            '            for name in $BINARY; do\n'
            '              executables+=("$RUNNER_TEMP/extracted/$target/$name$suffix")\n'
            '            done\n'
            '          done\n'
            '          bash "$GITHUB_WORKSPACE/policy/scripts/audit_binaries.sh" "$RUNNER_TEMP/advisory-db" \\\n'
            '            "${executables[@]}"\n',
            audit)
        self.assertNotIn("working-directory:", audit)
        self.assertEqual(1, admit.count("install-action"))

    def test_scheduled_ci_audits_each_binary_canary_release(self) -> None:
        # A job of its own: as steps after the live canary audit in checks, the
        # audit was skipped whenever a consumer's pin lacked release evidence.
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        jobs = self.mapping_block(ci, "jobs", indent=0)
        job = self.mapping_block(jobs, "release-audit", indent=2)
        self.assertNotIn("cargo-audit", self.mapping_block(jobs, "checks", indent=2))
        self.assertIn(
            "    if: >-\n      github.event_name == 'schedule' ||\n"
            "      (github.event_name == 'workflow_dispatch' && inputs.live-canary-audit)\n",
            job)
        self.assertNotIn("needs:", job)
        install = self.step(job, "Install the pinned cargo-audit")
        audit = self.step(job, "Audit each binary canary's executables against RustSec")
        self.assert_pinned_cargo_audit(install)
        self.assertLess(job.index(install), job.index(audit))
        self.assertIn(
            'bash scripts/audit_binary_canaries.sh canaries.json\n'
            '          "$RUNNER_TEMP/binary-canaries" "$RUNNER_TEMP/advisory-db"\n',
            audit)
        self.assertEqual(1, ci.count("audit_binary_canaries.sh"))
        self.assertNotIn("audit_binaries.sh", ci)
        self.assertIn("--exempt release-audit", self.mapping_block(jobs, "ci-gates", indent=2))

    def test_audit_script_fails_closed_without_an_ignore_list(self) -> None:
        script = AUDIT.read_text(encoding="utf-8")
        self.assertIn(
            'work="$(mktemp -d)"\ncd "$work"\n'
            'cargo-audit audit --db "$db" --no-yanked --deny warnings bin "$@" 2>&1 | tee audit.log\n',
            script)
        self.assertIn("set -euo pipefail\n", script)
        for text in (script, self.workflow, (WORKFLOWS / "ci.yml").read_text(encoding="utf-8"),
                     CANARY_AUDIT.read_text(encoding="utf-8")):
            for waiver in ("--ignore", "--no-fetch", "--stale", "--file", "--url"):
                with self.subTest(waiver=waiver):
                    self.assertNotIn(waiver, text)

    def test_publisher_gates_attests_and_publishes_the_fixed_assets(self) -> None:
        publish = self.job("publish")
        self.assert_in_order(
            publish,
            "Require a release-ready tag, repository and archive contract",
            "Require the Cargo package, binary and toolchain to match the release",
            "Build deterministic source archives",
            "Validate and admit each target's archive",
            "Download the admit job's archives and SBOMs",
            "Accept the admit job's SBOMs once its archives match this job's",
            "Require each binary SBOM to list the package",
            "Write and verify the exact release asset inventory",
            "Attest release asset provenance",
            "Write the provenance bundle as a release asset",
            "Verify provenance and SBOM attestations before publication",
            "Re-check the remote tag, main and release absence",
            "Create, inspect and publish the immutable release",
        )
        self.assertIn("run_archive_release_gates", publish)
        self.assertIn('signer="ryanduguid/release-policy/.github/workflows/release-binary.yml"', publish)
        self.assertIn('--signer-digest "$MODULE_SHA"', publish)
        self.assertIn('--bundle "$base.intoto.jsonl"', publish)
        inventory = self.step(publish, "Write and verify the exact release asset inventory")
        self.assertNotIn("intoto", inventory)
        self.assertIn('binary_release.py" assets', inventory)
        self.assertIn('binary_release.py" assets', self.step(
            publish, "Verify provenance and SBOM attestations before publication"))
        provenance = self.step(publish, "Attest release asset provenance")
        # Every handed-over file and SHA256SUMS.
        subjects = re.findall(r"^            \$\{\{.*?/dist/(.*)$", provenance, re.MULTILINE)
        base = "${{ steps.release.outputs.stem }}-${{ steps.release.outputs.version }}"
        self.assertEqual(
            sorted([*(base + suffix for suffix in binary_release.HANDOFF.values()), "SHA256SUMS"]),
            sorted(subjects))
        self.assertEqual(1 + len(TARGETS), publish.count("sbom-path:"))
        for target, _, label in TARGETS:
            with self.subTest(target=target):
                attest = self.step(publish, f"Attest the {label} binary's SPDX SBOM")
                self.assertIn(f"-{target}.spdx.json\n", attest)
        self.assertIn('bash "$GITHUB_WORKSPACE/policy/scripts/publish_binaries.sh"', publish)

    def test_publication_script_uploads_exactly_the_inventory(self) -> None:
        self.assertIn("assets=(\n", self.publisher)
        listed = self.publisher.split("assets=(\n", 1)[1].split("\n)\n", 1)[0].split()
        self.assertEqual(sorted(ASSETS), sorted(name.strip('"') for name in listed))
        uploads = re.findall(r'^upload_asset "(\$[a-z0-9_]+(?:\.[a-z.]+)?)" ', self.publisher,
                             re.MULTILINE)
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


@unittest.skipUnless(shutil.which("bash"), "bash required")
class SbomTargetCheckTests(YamlContractAssertions, unittest.TestCase):
    COMMON = ("release-policy", "regex", "regex-automata")
    LISTS = {
        "linux": ("linux-raw-sys", "rustix"),
        "linux-arm64": ("linux-raw-sys", "rustix", "errno"),
        "macos-arm64": ("core-foundation-sys", "errno"),
        "windows": ("windows-sys",),
    }

    def check(self, lists: dict[str, tuple[str, ...]]) -> subprocess.CompletedProcess[str]:
        jobs = self.mapping_block((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"), "jobs", indent=0)
        job = self.mapping_block(jobs, "binary-sbom-check", indent=2)
        step = job[job.index("      - name: Require each SBOM to describe its own target's dependencies\n"):]
        script = re.match(r"(?s).*?\n        run: \|\n((?:          .*\n|\n)+)", step)
        assert script is not None
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        shim = Path(temporary, "bin", "python")  # the step runs `python`
        shim.parent.mkdir()
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        for name, crates in lists.items():
            packages = [{"name": crate} for crate in (*self.COMMON, *crates)]
            Path(temporary, f"{name}.spdx.json").write_text(json.dumps({"packages": packages}))
        env = {**os.environ, "RUNNER_TEMP": temporary,
               "PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run(  # nosec B603 B607  # nosemgrep
            ["bash", "-c", re.sub(r"(?m)^ {10}", "", script.group(1))],
            cwd=ROOT, env=env, check=False, capture_output=True, text=True)

    def test_each_target_listing_its_own_crates_passes(self) -> None:
        self.assertEqual(0, self.check(self.LISTS).returncode)

    def test_swapped_macos_and_linux_arm64_lists_fail(self) -> None:
        for sbom, other in (("macos-arm64", "linux-arm64"), ("linux-arm64", "macos-arm64")):
            with self.subTest(sbom=sbom):
                self.assertNotEqual(0, self.check({**self.LISTS, sbom: self.LISTS[other]}).returncode)


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


# Stands in for gh: lists and downloads the fabricated release, and verifies
# an attestation unless the case says it is unsigned. Each call is logged.
FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "release view") ls "$FAKE_RELEASE" ;;
  "release download")
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --dir) dir="$2"; shift ;;
        --pattern) patterns+=("$2"); shift ;;
      esac
      shift
    done
    for pattern in "${patterns[@]}"; do cp "$FAKE_RELEASE/$pattern" "$dir/"; done ;;
  "attestation verify")
    [ "$FAKE_CASE" != unsigned ] && { [ "$FAKE_CASE" != unsigned-sums ] || [ "$3" != SHA256SUMS ]; } ;;
  *) exit 99 ;;
esac
"""
EVIDENCE: dict[str, object] = {
    "head_sha": "3" * 40, "policy_sha": "2" * 40, "ref": "v1.2.3", "run_id": 123,
    "started_at": "2026-10-28T21:33:02Z",
}
CANARY: dict[str, object] = {
    "current_policy_sha": "1" * 40, "family": "binary", "evidence": EVIDENCE,
    "policy_workflow": ".github/workflows/release-binary.yml",
    "repository": "ryanduguid/example", "workflow_path": ".github/workflows/release.yml",
}


@unittest.skipUnless(os.name == "posix" and all(
    shutil.which(tool) for tool in ("bash", "jq", "unzip", "od", "sha256sum")),
    "the release audit runs on Linux, with bash, jq, unzip, od and sha256sum")
class BinaryCanaryAuditTests(unittest.TestCase):
    """The scheduled audit of each binary canary's recorded release, with gh and cargo-audit faked."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        for name, text in (("gh", FAKE_GH), ("cargo-audit", FAKE_CARGO_AUDIT)):
            (fake_bin / name).write_text(text, encoding="utf-8", newline="\n")
            (fake_bin / name).chmod(0o755)
        self.release = self.root / "release"
        self.release.mkdir()
        # Each archive holds executables beside documents and a data file;
        # only the executables, known by their magic, are audited.
        elf, macho, pe = b"\x7fELF fabricated", b"\xcf\xfa\xed\xfe fabricated", b"MZ fabricated"
        documents = {"LICENSE": b"licence\n", "README.md": b"# example\n", "ca.pem": b"-----\n"}
        self.archive("example-1.2.3-x86_64-unknown-linux-musl.tar.gz",
                     {"example": elf, "example-helper": elf, **documents})
        self.archive("example-1.2.3-aarch64-apple-darwin.tar.gz", {"example": macho, **documents})
        self.archive("example-1.2.3-x86_64-pc-windows-msvc.zip", {"example.exe": pe, **documents})
        self.archive("example-1.2.3.tar.gz", {"src.rs": elf})
        self.write_sums()
        (self.release / "example-1.2.3.intoto.jsonl").write_text("{}\n", encoding="utf-8")
        self.manifest = self.root / "canaries.json"
        self.write_manifest([CANARY, {**CANARY, "family": "python",
                                      "repository": "ryanduguid/other"}])
        self.extracted = self.root / "work" / "ryanduguid-example" / "extracted"
        self.env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CARGO_HOME": str(self.root / "cargo-home"),
            "FAKE_LOG": str(self.root / "audit-calls"),
            "FAKE_GH_LOG": str(self.root / "gh-calls"),
            "FAKE_RELEASE": str(self.release),
        }

    def archive(self, name: str, members: dict[str, bytes]) -> None:
        path = self.release / name
        if name.endswith(".zip"):
            with zipfile.ZipFile(path, "w") as opened:
                for member, data in members.items():
                    opened.writestr(member, data)
            return
        with tarfile.open(path, "w:gz") as opened:
            for member, data in members.items():
                info = tarfile.TarInfo(member)
                info.size, info.mode = len(data), 0o755
                opened.addfile(info, io.BytesIO(data))

    def write_sums(self) -> None:
        sums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                       for path in sorted(self.release.iterdir())
                       if path.name.endswith((".tar.gz", ".zip")))
        (self.release / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")

    def write_manifest(self, families: list[dict[str, object]]) -> None:
        self.manifest.write_text(json.dumps({"families": families, "schema": 1}),
                                 encoding="utf-8")

    def audit(self, case: str = "clean") -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # nosec B603 B607  # nosemgrep
            ["bash", str(CANARY_AUDIT), str(self.manifest), str(self.root / "work"),
             str(self.root / "advisory-db")],
            cwd=self.root, env={**self.env, "FAKE_CASE": case},
            check=False, capture_output=True, text=True,
        )

    def test_every_executable_of_the_recorded_release_is_verified_then_audited(self) -> None:
        result = self.audit()
        self.assertEqual(0, result.returncode, result.stderr)
        arguments = (self.root / "audit-calls").read_text(encoding="utf-8").split("--listing\n")[0]
        self.assertEqual(
            sorted(arguments.splitlines()[7:]),
            sorted(str(self.extracted / archive / name) for archive, name in (
                ("example-1.2.3-x86_64-unknown-linux-musl.tar.gz", "example"),
                ("example-1.2.3-x86_64-unknown-linux-musl.tar.gz", "example-helper"),
                ("example-1.2.3-aarch64-apple-darwin.tar.gz", "example"),
                ("example-1.2.3-x86_64-pc-windows-msvc.zip", "example.exe"),
            )))
        calls = (self.root / "gh-calls").read_text(encoding="utf-8").splitlines()
        self.assertEqual("release view v1.2.3 --repo ryanduguid/example --json assets "
                         "--jq .assets[].name", calls[0])
        verifies = [call for call in calls if call.startswith("attestation verify ")]
        # SHA256SUMS first, since its lines name the archives, then each archive.
        self.assertEqual(4, len(verifies))
        self.assertTrue(verifies[0].startswith("attestation verify SHA256SUMS "), verifies[0])
        for call in verifies:
            self.assertIn(
                " --repo ryanduguid/example --bundle example-1.2.3.intoto.jsonl"
                f" --source-ref refs/tags/v1.2.3 --source-digest {'3' * 40}"
                " --signer-workflow ryanduguid/release-policy/.github/workflows/release-binary.yml"
                f" --signer-digest {'2' * 40}", call)
        self.assertFalse(any("ryanduguid/other" in call for call in calls))

    def test_a_planted_advisory_fails_the_binary_family(self) -> None:
        result = self.audit("advisory")
        self.assertEqual(1, result.returncode)
        self.assertIn("1 vulnerability found", result.stdout + result.stderr)

    def test_an_archive_that_is_not_the_signed_one_is_never_audited(self) -> None:
        archive = self.release / "example-1.2.3-aarch64-apple-darwin.tar.gz"
        for case, damage in (
            ("unsigned", lambda: None),
            ("clean", lambda: archive.write_bytes(archive.read_bytes() + b"\0")),
        ):
            with self.subTest(case=case):
                damage()
                shutil.rmtree(self.root / "work", ignore_errors=True)
                self.assertNotEqual(0, self.audit(case).returncode)
                self.assertFalse((self.root / "audit-calls").exists())

    def test_the_archives_are_those_the_signed_sha256sums_lists(self) -> None:
        # The release's asset listing is not signed, so it cannot drop an archive.
        for case, damage in (
            ("unsigned-sums", lambda: None),
            ("clean", lambda: (self.release / "example-1.2.3-aarch64-apple-darwin.tar.gz").unlink()),
        ):
            with self.subTest(case=case):
                damage()
                shutil.rmtree(self.root / "work", ignore_errors=True)
                self.assertNotEqual(0, self.audit(case).returncode)
                self.assertFalse((self.root / "audit-calls").exists())

    def test_a_last_sha256sums_line_without_a_newline_is_read(self) -> None:
        sums = self.release / "SHA256SUMS"
        lines = sums.read_text(encoding="utf-8").splitlines()
        linux = [line for line in lines if line.endswith("-x86_64-unknown-linux-musl.tar.gz")]
        sums.write_text("\n".join([*(line for line in lines if line not in linux), *linux]),
                        encoding="utf-8", newline="\n")
        result = self.audit()
        self.assertEqual(0, result.returncode, result.stderr)
        archive = self.extracted / "example-1.2.3-x86_64-unknown-linux-musl.tar.gz"
        self.assertIn(f"{archive / 'example-helper'}\n",
                      (self.root / "audit-calls").read_text(encoding="utf-8"))

    def test_an_archive_without_an_executable_fails(self) -> None:
        self.archive("example-1.2.3-aarch64-apple-darwin.tar.gz",
                     {"LICENSE": b"licence\n", "README.md": b"# example\n"})
        self.write_sums()
        result = self.audit()
        self.assertEqual(1, result.returncode)
        self.assertIn("no executable in example-1.2.3-aarch64-apple-darwin.tar.gz", result.stderr)
        self.assertFalse((self.root / "audit-calls").exists())

    def test_a_malformed_canary_entry_fails_before_github_is_asked(self) -> None:
        evidence = EVIDENCE
        for name, entry in (
            ("no policy digest", {**CANARY, "evidence": {
                key: value for key, value in evidence.items() if key != "policy_sha"}}),
            ("empty source digest", {**CANARY, "evidence": {**evidence, "head_sha": ""}}),
            ("short source digest", {**CANARY, "evidence": {**evidence, "head_sha": "3" * 39}}),
            ("tag not a version", {**CANARY, "evidence": {**evidence, "ref": "main"}}),
            ("repository not owner/name", {**CANARY, "repository": "example"}),
        ):
            with self.subTest(name):
                self.write_manifest([entry])
                shutil.rmtree(self.root / "work", ignore_errors=True)
                result = self.audit()
                self.assertEqual(1, result.returncode)
                self.assertIn("malformed binary canary", result.stderr)
                self.assertFalse((self.root / "gh-calls").exists())

    def test_a_manifest_or_release_without_a_binary_canary_fails(self) -> None:
        self.write_manifest([{**CANARY, "family": "python"}])
        result = self.audit()
        self.assertEqual(1, result.returncode)
        self.assertIn("records no binary canary", result.stderr)
        self.write_manifest([CANARY])
        (self.release / "example-1.2.3.intoto.jsonl").unlink()
        shutil.rmtree(self.root / "work")
        result = self.audit()
        self.assertEqual(1, result.returncode)
        self.assertIn("one provenance bundle", result.stderr)
        self.assertFalse((self.root / "audit-calls").exists())


if __name__ == "__main__":
    unittest.main()
