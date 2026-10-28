"""Contract of the Rust binary release family: release-binary.yml and its publisher.

The workflow is the trust boundary: consumer code runs only in read-only build
jobs, and the one privileged job admits their archives as untrusted input,
never runs Cargo, and publishes a fixed asset set under one signer identity.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404: runs this file's own entry point
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_skill_workflows import YamlContractAssertions  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
LINUX = "x86_64-unknown-linux-musl"
WINDOWS = "x86_64-pc-windows-msvc"
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
            "cargo ", "rustup", "build_binary.sh", "install-action", "actions/upload-artifact@",
            "pip install", "unittest discover", "pattern:", "merge-multiple", "./scripts/",
            "gh release upload", "secrets: inherit",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, publish)
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


if __name__ == "__main__":
    unittest.main()
