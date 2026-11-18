# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""What each RustSec audit lane denies, and how CI audits the Cargo lockfiles.

A vulnerable, unsound or yanked crate fails pull requests and pushes (the
change lane), a binary release (the release lane) and the weekly run (the
scheduled lane). An unmaintained crate fails only the weekly run (Ryan, 18
November 2026). The verdicts come from the pinned cargo-audit itself, against
a fixture advisory database; CI's rustsec job runs them and requires the tool.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404: runs bash, git and cargo-audit on fixtures
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_skill_workflows import YamlContractAssertions  # noqa: E402

LANES = ROOT / "scripts" / "rustsec_lanes.sh"
LOCKFILE_AUDIT = ROOT / "scripts" / "audit_lockfiles.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"
LANE_NAMES = ("change", "release", "scheduled")
# Each fixture advisory against the locked crate, by its informational kind
# (None for a vulnerability), and each lane's expected exit status in
# LANE_NAMES order.
VERDICTS = {
    "unmaintained": (0, 0, 1),
    "unsound": (1, 1, 1),
    None: (1, 1, 1),
}
LOCKFILE = """version = 4

[[package]]
name = "itoa"
version = "1.0.11"
source = "registry+https://github.com/rust-lang/crates.io-index"
"""


def advisory(package: str, informational: str | None) -> str:
    kind = f'informational = "{informational}"\n' if informational else ""
    return (
        "```toml\n[advisory]\n"
        'id = "RUSTSEC-2099-0001"\n'
        f'package = "{package}"\n'
        'date = "2026-11-18"\n'
        'url = "https://example.invalid/RUSTSEC-2099-0001"\n'
        f"{kind}\n[versions]\npatched = []\n```\n\n# Fixture advisory\n"
    )


def deny_arguments(lane: str) -> list[str]:
    result = subprocess.run(  # nosec B603 B607  # nosemgrep
        ["bash", "-c", '. "$1" && deny_arguments "$2"', "_", str(LANES), lane],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.split()


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "bash and git required")
class LaneVerdictTests(unittest.TestCase):
    """The pinned cargo-audit's verdict on each lane's --deny arguments."""

    def setUp(self) -> None:
        self.cargo_audit = shutil.which("cargo-audit")
        if self.cargo_audit is None:
            if os.environ.get("RUSTSEC_LANES_REQUIRE_CARGO_AUDIT") == "1":
                self.fail("cargo-audit is required here but is not on PATH")
            self.skipTest("cargo-audit not installed")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lockfile = self.root / "Cargo.lock"
        self.lockfile.write_text(LOCKFILE, encoding="utf-8", newline="\n")
        # Its own Cargo home, so no audit.toml or index cache of the caller's applies.
        self.env = {**os.environ, "CARGO_HOME": str(self.root / "cargo-home")}

    def database(self, package: str, informational: str | None) -> Path:
        db = self.root / f"db-{package}-{informational}"
        crate = db / "crates" / package
        crate.mkdir(parents=True)
        (crate / "RUSTSEC-2099-0001.md").write_text(
            advisory(package, informational), encoding="utf-8", newline="\n")
        for command in (["init", "-q"], ["add", "-A"],
                        ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(db), *command],  # nosec B603 B607  # nosemgrep
                           check=True, capture_output=True)
        return db

    def verdict(self, db: Path, lane: str) -> subprocess.CompletedProcess[str]:
        assert self.cargo_audit is not None
        work = Path(tempfile.mkdtemp(dir=self.root))
        return subprocess.run(  # nosec B603  # nosemgrep
            [self.cargo_audit, "audit", "--no-fetch", "--db", str(db), *deny_arguments(lane),
             "--file", str(self.lockfile)],
            cwd=work, env=self.env, check=False, capture_output=True, text=True,
        )

    def test_an_advisory_for_another_crate_passes_every_lane(self) -> None:
        db = self.database("not-in-the-lockfile", None)
        for lane in LANE_NAMES:
            with self.subTest(lane=lane):
                result = self.verdict(db, lane)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_each_lane_fails_exactly_the_kinds_it_denies(self) -> None:
        for informational, expected in VERDICTS.items():
            db = self.database("itoa", informational)
            for lane, status in zip(LANE_NAMES, expected, strict=True):
                with self.subTest(advisory=informational or "vulnerability", lane=lane):
                    result = self.verdict(db, lane)
                    self.assertEqual(status, result.returncode, result.stdout + result.stderr)
                    self.assertIn("RUSTSEC-2099-0001", result.stdout + result.stderr)


class LaneArgumentTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "bash required")
    def test_an_unknown_lane_is_refused(self) -> None:
        for lane in ("", "push", "Scheduled"):
            with self.subTest(lane=lane):
                result = subprocess.run(  # nosec B603 B607  # nosemgrep
                    ["bash", "-c", '. "$1" && deny_arguments "$2"', "_", str(LANES), lane],
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual(64, result.returncode)
                self.assertEqual("", result.stdout)


# Stands in for cargo-audit: logs its working directory and arguments, then
# behaves as the real tool does for the case under test.
FAKE_CARGO_AUDIT = """#!/usr/bin/env bash
printf '%s %s\\n' "$PWD" "$*" >> "$FAKE_LOG"
mkdir -p "$3"
if [ "$FAKE_CASE" = yanked-unchecked ]; then
  echo "error: couldn't check if the package is yanked: timed out" >&2
fi
if [ -n "$FAKE_VULNERABLE" ] && [[ "${!#}" == */"$FAKE_VULNERABLE" ]]; then
  echo "error: 1 vulnerability found!" >&2
  exit 1
fi
"""


@unittest.skipUnless(shutil.which("bash"), "bash required")
class LockfileAuditScriptTests(unittest.TestCase):
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
        # Relative to the caller's directory, as CI names them.
        self.lockfiles = ["Cargo.lock", "fuzz/Cargo.lock"]
        for lockfile in self.lockfiles:
            (self.caller / lockfile).parent.mkdir(exist_ok=True)
            (self.caller / lockfile).write_text(LOCKFILE, encoding="utf-8")
        self.cargo_home = self.root / "cargo-home"
        self.cargo_home.mkdir()
        self.log = self.root / "calls"
        self.db = (self.root / "advisory-db").as_posix()
        self.env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CARGO_HOME": str(self.cargo_home),
            "FAKE_LOG": str(self.log),
        }

    def audit(self, *arguments: str, case: str = "clean",
              vulnerable: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # nosec B603 B607  # nosemgrep
            ["bash", str(LOCKFILE_AUDIT), *arguments], cwd=self.caller,
            env={**self.env, "FAKE_CASE": case, "FAKE_VULNERABLE": vulnerable},
            check=False, capture_output=True, text=True,
        )

    def calls(self) -> list[tuple[str, str]]:
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [tuple(line.split(" ", 1)) for line in lines]  # type: ignore[misc]

    def test_each_lockfile_gets_a_fresh_database_and_the_lane_arguments(self) -> None:
        for lane, deny in (("change", "--deny unsound --deny yanked"),
                           ("scheduled", "--deny warnings")):
            with self.subTest(lane=lane):
                shutil.rmtree(self.db, ignore_errors=True)
                self.log.unlink(missing_ok=True)
                result = self.audit(lane, self.db, *self.lockfiles)
                self.assertEqual(0, result.returncode, result.stderr)
                calls = self.calls()
                self.assertEqual(len(self.lockfiles), len(calls))
                for n, ((directory, arguments), lockfile) in enumerate(
                        zip(calls, self.lockfiles, strict=True), start=1):
                    # Read from a new empty directory, so by an absolute path.
                    self.assertFalse(Path(directory, ".cargo").exists(), directory)
                    prefix = f"audit --db {self.db}/{n} {deny} --file /"
                    self.assertTrue(arguments.startswith(prefix), arguments)
                    self.assertTrue(arguments.endswith(f"/caller/{lockfile}"), arguments)

    def test_every_lockfile_is_audited_before_a_failure_is_reported(self) -> None:
        result = self.audit("change", self.db, *self.lockfiles, vulnerable="caller/Cargo.lock")
        self.assertEqual(1, result.returncode)
        self.assertEqual(2, len(self.calls()))
        failures = result.stderr.split("RustSec audit failed for ")[1:]
        self.assertEqual(1, len(failures), result.stderr)
        self.assertTrue(failures[0].endswith("/caller/Cargo.lock\n"), failures)

    def test_a_skipped_yanked_check_fails(self) -> None:
        result = self.audit("change", self.db, *self.lockfiles, case="yanked-unchecked")
        self.assertEqual(1, result.returncode)
        self.assertIn("could not check for yanked crates", result.stderr)
        self.assertEqual(2, result.stderr.count("RustSec audit failed for "))

    def test_refuses_before_auditing(self) -> None:
        for arguments in (
            ("change", self.db),
            ("release", self.db, *self.lockfiles),
            ("weekly", self.db, *self.lockfiles),
            ("change", self.db, "missing/Cargo.lock"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(64, self.audit(*arguments).returncode)
        Path(self.db).mkdir()
        self.assertIn("must be fetched fresh", self.audit("change", self.db, *self.lockfiles).stderr)
        Path(self.db).rmdir()
        (self.cargo_home / "audit.toml").write_text("", encoding="utf-8")
        refused = self.audit("change", self.db, *self.lockfiles)
        self.assertEqual(1, refused.returncode)
        self.assertIn("refusing to audit with", refused.stderr)
        self.assertFalse(self.log.exists())


class CiLaneTests(YamlContractAssertions, unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = self.mapping_block(CI.read_text(encoding="utf-8"), "jobs", indent=0)

    def test_the_lockfile_audit_runs_on_every_event_and_gates_ci(self) -> None:
        job = self.mapping_block(self.jobs, "rustsec", indent=2)
        self.assertNotIn("\n    if:", job)
        self.assertIn("rustsec", self.needs_values_of("ci-gates"))
        self.assertNotIn("--exempt rustsec", self.mapping_block(self.jobs, "ci-gates", indent=2))
        install = self.step_block(job, "Install the pinned cargo-audit")
        self.assertIn("tool: cargo-audit@0.22.2\n", install)
        self.assertIn("fallback: none\n", install)
        verdicts = self.step_block(job, "Check each lane's verdicts with the pinned cargo-audit")
        self.assertIn('RUSTSEC_LANES_REQUIRE_CARGO_AUDIT: "1"\n', verdicts)
        self.assertIn("run: python -m unittest tests.test_rustsec_lanes\n", verdicts)
        audit = self.step_block(job, "Audit each Cargo lockfile")
        self.assertIn(
            "LANE: ${{ github.event_name == 'schedule' && 'scheduled' || 'change' }}\n", audit)
        self.assertIn('bash scripts/audit_lockfiles.sh "$LANE" "$RUNNER_TEMP/advisory-db"', audit)
        self.assertLess(job.index(install), job.index(verdicts))
        self.assertLess(job.index(verdicts), job.index(audit))

    def test_every_tracked_lockfile_is_audited(self) -> None:
        tracked = subprocess.run(  # nosec B603 B607  # nosemgrep
            ["git", "-C", str(ROOT), "ls-files", "--", "Cargo.lock", "*/Cargo.lock"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertTrue(tracked)
        audit = self.step_block(self.mapping_block(self.jobs, "rustsec", indent=2),
                                "Audit each Cargo lockfile")
        self.assertEqual(sorted(tracked),
                         sorted(word for word in audit.split() if word.endswith("Cargo.lock")))

    def step_block(self, job: str, name: str) -> str:
        marker = f"      - name: {name}\n"
        self.assertEqual(1, job.count(marker), f"step is missing or repeated: {name}")
        start = job.index(marker)
        end = job.find("\n      - name:", start + len(marker))
        return job[start:] if end < 0 else job[start:end]

    def needs_values_of(self, job: str) -> list[str]:
        block = self.mapping_block(self.jobs, job, indent=2)
        line = next(line for line in block.splitlines() if line.startswith("    needs: ["))
        return [name.strip() for name in line.split("[", 1)[1].rstrip("]").split(",")]


if __name__ == "__main__":
    unittest.main()
