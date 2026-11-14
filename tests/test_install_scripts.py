# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""The release installers, scripts/install.sh and scripts/install.ps1.

Each runs against a fabricated release laid out as release-binary.yml
publishes it, with the download and the GitHub CLI faked. Nothing may be
installed unless both attestations pass, the archive matches SHA256SUMS and
the platform has a build.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess  # nosec B404: runs the installers under test
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO, TAG, VERSION, STEM = "owner/example-tool", "v1.2.3", "1.2.3", "example-tool"
SIGNER = "ryanduguid/release-policy/.github/workflows/release-binary.yml"
LINUX = "x86_64-unknown-linux-musl"
WINDOWS = "x86_64-pc-windows-msvc"
TARGETS = (LINUX, "aarch64-unknown-linux-musl", "aarch64-apple-darwin", WINDOWS)
DOCUMENTS = {"LICENSE": b"licence\n", "ca.pem": b"fabricated bundle\n"}

# Logs each call, then refuses the subject named in FAKE_GH_REFUSE.
FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
[ "${3##*/}" != "${FAKE_GH_REFUSE:-}" ]
"""
# Serves FAKE_RELEASE by asset name. FAKE_CUT names an asset that arrives
# as a few bytes, with exit status FAKE_CUT_STATUS.
FAKE_CURL = """#!/bin/sh
while [ "$#" -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; *) url=$1 ;; esac
  shift
done
printf '%s\\n' "$url" >> "$FAKE_LOG"
name=${url##*/}
[ -f "$FAKE_RELEASE/$name" ] || exit 22
if [ "$name" = "${FAKE_CUT:-}" ]; then
  printf partial > "$out"
  exit "$FAKE_CUT_STATUS"
fi
cp "$FAKE_RELEASE/$name" "$out"
"""
FAKE_UNAME = """#!/bin/sh
case "$1" in -s) echo "$FAKE_OS" ;; -m) echo "$FAKE_ARCH" ;; esac
"""
# The same fakes as PowerShell functions, which take precedence over the
# cmdlet and over any gh on PATH.
FAKE_POWERSHELL = """
function global:Invoke-WebRequest([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing) {
    Add-Content -LiteralPath $env:FAKE_LOG -Value $Uri
    $name = $Uri.Substring($Uri.LastIndexOf('/') + 1)
    $source = Join-Path $env:FAKE_RELEASE $name
    if (-not (Test-Path -LiteralPath $source)) { throw "404 $name" }
    if ($name -eq $env:FAKE_CUT) {
        Set-Content -LiteralPath $OutFile -Value 'partial'
        if ($env:FAKE_CUT_STATUS -ne '0') { throw 'connection reset' }
        return
    }
    Copy-Item -LiteralPath $source -Destination $OutFile
}
if ($env:FAKE_GH -eq '1') {
    function global:gh {
        Add-Content -LiteralPath $env:FAKE_LOG -Value ($args -join ' ')
        $global:LASTEXITCODE = [int]((Split-Path -Leaf $args[2]) -eq $env:FAKE_GH_REFUSE)
    }
}
# A gh that runs nothing, after an earlier command left a zero exit code.
if ($env:FAKE_GH -eq 'silent') {
    function global:gh { }
    $global:LASTEXITCODE = 0
}
"""


class InstallerCases:
    """Cases both installers share; a subclass runs one installer on its platform."""

    TARGET = LINUX
    EXECUTABLE = STEM

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = self.root / "release"
        self.release.mkdir()
        for target in TARGETS:
            executable = STEM + (".exe" if target == WINDOWS else "")
            self.archive(target, {executable: target.encode(), **DOCUMENTS})
        self.write_sums()
        (self.release / f"{STEM}-{VERSION}.intoto.jsonl").write_text("{}\n", encoding="utf-8")
        self.temp = self.root / "temp"
        self.temp.mkdir()
        self.log = self.root / "calls"
        self.destination = self.root / "installed"
        self.env = {
            **os.environ,
            "FAKE_LOG": str(self.log), "FAKE_RELEASE": str(self.release),
            "FAKE_GH_REFUSE": "", "FAKE_CUT": "", "FAKE_CUT_STATUS": "0",
            "TMPDIR": str(self.temp), "TEMP": str(self.temp), "TMP": str(self.temp),
        }

    def archive(self, target: str, members: dict[str, bytes], link: str = "") -> None:
        path = self.release / f"{STEM}-{VERSION}-{target}"
        if target == WINDOWS:
            with zipfile.ZipFile(f"{path}.zip", "w") as opened:
                for member, data in members.items():
                    opened.writestr(member, data)
            return
        with tarfile.open(f"{path}.tar.gz", "w:gz") as opened:
            for member, data in members.items():
                info = tarfile.TarInfo(member)
                info.size, info.mode = len(data), 0o755
                opened.addfile(info, io.BytesIO(data))
            if link:
                info = tarfile.TarInfo(link)
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                opened.addfile(info)

    def write_sums(self, extra: str = "") -> None:
        sums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                       for path in sorted(self.release.iterdir())
                       if path.name.endswith((".tar.gz", ".zip")))
        (self.release / "SHA256SUMS").write_text(sums + extra, encoding="utf-8", newline="\n")

    @property
    def asset(self) -> str:
        return f"{STEM}-{VERSION}-{self.TARGET}" + (".zip" if self.TARGET == WINDOWS else ".tar.gz")

    def install(self, *arguments: str, **env: str) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def assert_installed(self, result: subprocess.CompletedProcess[str], tag: str = TAG) -> None:
        self.assertEqual(0, result.returncode, result.stderr)
        executable = self.destination / self.EXECUTABLE
        self.assertEqual(self.TARGET.encode(), executable.read_bytes())
        self.assertEqual({self.EXECUTABLE, *DOCUMENTS}, {p.name for p in self.destination.iterdir()})
        base = f"https://github.com/{REPO}/releases/download/{tag}"
        bundle = f"{STEM}-{VERSION}.intoto.jsonl"
        calls = self.log.read_text(encoding="utf-8").replace("\\", "/").splitlines()
        self.assertEqual([f"{base}/SHA256SUMS", f"{base}/{self.asset}", f"{base}/{bundle}"],
                         calls[:3])
        self.assertEqual(5, len(calls), calls)
        work = calls[3].split()[2].rsplit("/", 1)[0]
        self.assertTrue(work.startswith(self.temp.as_posix()), work)
        pins = (f"--repo {REPO} --signer-workflow {SIGNER} --source-ref refs/tags/{tag} "
                "--deny-self-hosted-runners")
        self.assertEqual([f"attestation verify {work}/{subject} --bundle {work}/{bundle} {pins}"
                          for subject in ("SHA256SUMS", self.asset)], calls[3:])
        self.assertEqual([], list(self.temp.iterdir()), "the work directory was left behind")

    def assert_refused(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(reason, result.stderr)
        self.assertFalse(self.destination.exists(), "a refused install wrote files")
        self.assertEqual([], list(self.temp.iterdir()), "the work directory was left behind")

    def test_installs_the_verified_archive(self) -> None:
        self.assert_installed(self.install(REPO, TAG, str(self.destination)))

    def test_installs_a_component_tag(self) -> None:
        tag = f"{STEM}/v{VERSION}"
        self.assert_installed(self.install(REPO, tag, str(self.destination)), tag)

    def test_replaces_an_earlier_install_and_keeps_other_files(self) -> None:
        self.destination.mkdir()
        (self.destination / self.EXECUTABLE).write_bytes(b"old")
        (self.destination / "notes.txt").write_bytes(b"mine")
        result = self.install(REPO, TAG, str(self.destination))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.TARGET.encode(), (self.destination / self.EXECUTABLE).read_bytes())
        self.assertEqual(b"mine", (self.destination / "notes.txt").read_bytes())

    def test_ignores_a_directory_in_the_way_of_a_staging_name(self) -> None:
        (self.destination / f".{self.EXECUTABLE}.new").mkdir(parents=True)
        result = self.install(REPO, TAG, str(self.destination))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.TARGET.encode(), (self.destination / self.EXECUTABLE).read_bytes())
        self.assertEqual({self.EXECUTABLE, f".{self.EXECUTABLE}.new", *DOCUMENTS},
                         {p.name for p in self.destination.iterdir()})

    def test_refuses_an_archive_that_does_not_match_sha256sums(self) -> None:
        self.write_sums()
        (self.release / self.asset).write_bytes(b"tampered")
        self.assert_refused(self.install(REPO, TAG, str(self.destination)), "does not match SHA256SUMS")

    def test_refuses_a_truncated_download(self) -> None:
        result = self.install(REPO, TAG, str(self.destination), FAKE_CUT=self.asset)
        self.assert_refused(result, "does not match SHA256SUMS")

    def test_refuses_a_failed_download(self) -> None:
        for asset in ("SHA256SUMS", self.asset, f"{STEM}-{VERSION}.intoto.jsonl"):
            with self.subTest(asset=asset):
                result = self.install(REPO, TAG, str(self.destination),
                                      FAKE_CUT=asset, FAKE_CUT_STATUS="18")
                self.assert_refused(result, f"could not download {asset}")

    def test_refuses_a_release_without_its_provenance_bundle(self) -> None:
        (self.release / f"{STEM}-{VERSION}.intoto.jsonl").unlink()
        result = self.install(REPO, TAG, str(self.destination))
        self.assert_refused(result, f"could not download {STEM}-{VERSION}.intoto.jsonl")

    def test_refuses_a_failing_attestation(self) -> None:
        for subject in ("SHA256SUMS", self.asset):
            with self.subTest(subject=subject):
                result = self.install(REPO, TAG, str(self.destination), FAKE_GH_REFUSE=subject)
                self.assert_refused(result, f"{subject} failed provenance verification")

    def test_refuses_sha256sums_without_exactly_one_archive_for_the_platform(self) -> None:
        line = f"{'0' * 64}  other-{VERSION}-{self.asset.split(f'{VERSION}-', 1)[1]}\n"
        for sums in ("", line):
            with self.subTest(sums=sums):
                if sums:
                    self.write_sums(sums)
                else:
                    (self.release / "SHA256SUMS").write_text("", encoding="utf-8")
                result = self.install(REPO, TAG, str(self.destination))
                self.assert_refused(result, f"archives for {self.TARGET}, not 1")

    def test_refuses_a_malformed_sha256sums_line_before_downloading_more(self) -> None:
        digest = hashlib.sha256((self.release / self.asset).read_bytes()).hexdigest()
        for line in (f"{digest.upper()}  {self.asset}", f"{digest[1:]}  {self.asset}",
                     f"{digest}  {self.asset} extra", f"{digest} *{self.asset}",
                     f"{digest}  ../{self.asset}", f"{digest}  -{self.asset}"):
            with self.subTest(line=line):
                (self.release / "SHA256SUMS").write_text(line + "\n", encoding="utf-8", newline="\n")
                self.log.unlink(missing_ok=True)
                result = self.install(REPO, TAG, str(self.destination))
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertFalse(self.destination.exists(), "a refused install wrote files")
                self.assertEqual([f"https://github.com/{REPO}/releases/download/{TAG}/SHA256SUMS"],
                                 self.log.read_text(encoding="utf-8").splitlines())

    def test_refuses_an_unsafe_archive_member(self) -> None:
        for member in ("../escape", "sub/file", ".profile"):
            with self.subTest(member=member):
                self.archive(self.TARGET, {self.EXECUTABLE: b"x", member: b"x"})
                self.write_sums()
                result = self.install(REPO, TAG, str(self.destination))
                self.assert_refused(result, "holds an unexpected member")

    def test_refuses_malformed_arguments(self) -> None:
        for arguments in ((REPO,), ("owner", TAG), ("a/b/c", TAG), (REPO, "1.2.3"),
                          (REPO, "tool/../v1.2.3"), (REPO, "v1.2.3\nv9")):
            with self.subTest(arguments=arguments):
                result = self.install(*arguments)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(self.log.exists(), "a malformed argument reached the network")


@unittest.skipUnless(os.name == "posix" and all(shutil.which(tool) for tool in (
    "sh", "tar", "gzip", "mktemp", "rm", "cut", "mkdir", "cp", "mv")) and (
    shutil.which("sha256sum") or shutil.which("shasum")), "POSIX tools required")
class ShellInstallerTests(InstallerCases, unittest.TestCase):
    """install.sh on Linux and macOS, with PATH holding only the tools it may use."""

    def setUp(self) -> None:
        super().setUp()
        self.env.update({"PATH": str(self.tools()), "FAKE_OS": "Linux", "FAKE_ARCH": "x86_64",
                         "HOME": str(self.root / "home")})

    def tools(self, *omit: str) -> Path:
        """A directory of the fakes and the real tools install.sh may use, less those omitted."""
        directory = self.root / "-".join(("bin", *omit))
        directory.mkdir()
        for name, text in (("gh", FAKE_GH), ("curl", FAKE_CURL), ("uname", FAKE_UNAME)):
            if name not in omit:
                (directory / name).write_text(text, encoding="utf-8", newline="\n")
                (directory / name).chmod(0o755)
        for tool in ("tar", "gzip", "mktemp", "rm", "cut", "mkdir", "cp", "mv",
                     "sha256sum", "shasum"):
            if tool not in omit and (found := shutil.which(tool)):
                (directory / tool).symlink_to(found)
        return directory

    def install(self, *arguments: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # nosec B603  # nosemgrep
            [str(shutil.which("sh")), str(ROOT / "scripts" / "install.sh"), *arguments],
            env={**self.env, **env}, check=False, capture_output=True, text=True)

    def test_installs_each_platform_archive(self) -> None:
        for os_name, arch, target in (("Linux", "amd64", LINUX),
                                      ("Linux", "aarch64", "aarch64-unknown-linux-musl"),
                                      ("Linux", "arm64", "aarch64-unknown-linux-musl"),
                                      ("Darwin", "arm64", "aarch64-apple-darwin")):
            with self.subTest(os=os_name, arch=arch):
                self.TARGET = target
                destination = self.root / f"{os_name}-{arch}"
                result = self.install(REPO, TAG, str(destination), FAKE_OS=os_name, FAKE_ARCH=arch)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(target.encode(), (destination / STEM).read_bytes())
                self.assertTrue(os.access(destination / STEM, os.X_OK))

    def test_installs_into_the_data_directory_by_default(self) -> None:
        self.destination = self.root / "data" / STEM
        result = self.install(REPO, TAG, XDG_DATA_HOME=str(self.root / "data"))
        self.assert_installed(result)
        self.assertIn(f"Add {self.destination} to PATH", result.stdout)

    def test_refuses_a_platform_without_a_build(self) -> None:
        for os_name, arch in (("Darwin", "x86_64"), ("Linux", "riscv64"), ("FreeBSD", "amd64"),
                              ("MINGW64_NT-10.0", "x86_64")):
            with self.subTest(os=os_name, arch=arch):
                result = self.install(REPO, TAG, str(self.destination),
                                      FAKE_OS=os_name, FAKE_ARCH=arch)
                self.assert_refused(result, f"no build for {os_name} {arch}")
                self.assertFalse(self.log.exists())

    def test_refuses_an_archive_holding_a_symbolic_link(self) -> None:
        self.archive(self.TARGET, {self.EXECUTABLE: b"x"}, link="LICENSE")
        self.write_sums()
        self.assert_refused(self.install(REPO, TAG, str(self.destination)),
                            "holds LICENSE as something other than a file")

    def test_never_writes_through_a_link_in_the_install_directory(self) -> None:
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        self.destination.mkdir()
        (self.destination / f".{STEM}.new").symlink_to(victim)
        result = self.install(REPO, TAG, str(self.destination))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b"keep", victim.read_bytes())
        self.assertFalse((self.destination / STEM).is_symlink())
        self.assertEqual(self.TARGET.encode(), (self.destination / STEM).read_bytes())

    def test_refuses_without_each_required_tool(self) -> None:
        for tools, reason in ((("gh",), "the GitHub CLI (gh) is required"),
                              (("curl",), "curl is required"),
                              (("sha256sum", "shasum"), "sha256sum or shasum is required")):
            with self.subTest(tools=tools):
                result = self.install(REPO, TAG, str(self.destination), PATH=str(self.tools(*tools)))
                self.assert_refused(result, reason)


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 required")
class PowerShellInstallerTests(InstallerCases, unittest.TestCase):
    """install.ps1 as Windows x86_64 sees it, with PATH empty so only the fakes run."""

    TARGET = WINDOWS
    EXECUTABLE = f"{STEM}.exe"

    def setUp(self) -> None:
        super().setUp()
        empty = self.root / "empty"
        empty.mkdir()
        self.env = {key: value for key, value in self.env.items()
                    if key != "PROCESSOR_ARCHITEW6432"}
        self.env.update({"PATH": str(empty), "OS": "Windows_NT",
                         "PROCESSOR_ARCHITECTURE": "AMD64", "FAKE_GH": "1",
                         "LOCALAPPDATA": str(self.root / "local")})

    def install(self, *arguments: str, **env: str) -> subprocess.CompletedProcess[str]:
        quoted = " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
        script = str(ROOT / "scripts" / "install.ps1").replace("'", "''")
        return subprocess.run(  # nosec B603  # nosemgrep
            [str(shutil.which("pwsh")), "-NoProfile", "-NonInteractive", "-Command",
             f"{FAKE_POWERSHELL}\n& '{script}' {quoted}"],
            env={**self.env, **env}, check=False, capture_output=True, text=True)

    def test_installs_into_local_programs_by_default(self) -> None:
        self.destination = self.root / "local" / "Programs" / STEM
        result = self.install(REPO, TAG)
        self.assert_installed(result)
        self.assertIn("to PATH", result.stdout)

    def test_refuses_a_platform_without_a_build(self) -> None:
        for env, reason in (({"PROCESSOR_ARCHITECTURE": "ARM64"}, "no build for Windows ARM64"),
                            ({"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "ARM64"},
                             "no build for Windows ARM64"),
                            ({"OS": "Linux"}, "use install.sh on Linux and macOS")):
            with self.subTest(env=env):
                self.assert_refused(self.install(REPO, TAG, str(self.destination), **env), reason)
                self.assertFalse(self.log.exists())

    def test_refuses_when_gh_leaves_no_exit_code(self) -> None:
        result = self.install(REPO, TAG, str(self.destination), FAKE_GH="silent")
        self.assert_refused(result, "SHA256SUMS failed provenance verification")

    def test_refuses_without_the_github_cli(self) -> None:
        result = self.install(REPO, TAG, str(self.destination), FAKE_GH="0")
        self.assert_refused(result, "the GitHub CLI (gh) is required")
