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
WINDOWS_ARM64 = "aarch64-pc-windows-msvc"
TARGETS = (LINUX, "aarch64-unknown-linux-musl", "x86_64-apple-darwin", "aarch64-apple-darwin",
           WINDOWS, WINDOWS_ARM64)
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
# Answers sysctl.proc_translated with FAKE_TRANSLATED: 1 under Rosetta, 0
# natively; unset, the unknown name of an Intel Mac before macOS 11.
FAKE_SYSCTL = """#!/bin/sh
[ "$*" = "-n sysctl.proc_translated" ] || exit 2
[ -n "${FAKE_TRANSLATED:-}" ] || { echo "sysctl: unknown oid 'sysctl.proc_translated'" >&2; exit 1; }
echo "$FAKE_TRANSLATED"
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
# The user's Path value lives in the file FAKE_USER_PATH, never the registry.
# Each read and write of it is logged to FAKE_REGISTRY_LOG; FAKE_PATH_DENIED
# refuses the write. Every other Get-Item, such as Expand-Archive's, is real.
if ($env:FAKE_USER_PATH) {
    function global:Get-Item {
        [CmdletBinding()] param([string]$Path, [string]$LiteralPath)
        if ($LiteralPath -ne 'HKCU:\\Environment') {
            return Microsoft.PowerShell.Management\\Get-Item @PSBoundParameters
        }
        $key = [pscustomobject]@{}
        $key | Add-Member -MemberType ScriptMethod -Name GetValue -Value {
            param($Name, $Default, $Options)
            Add-Content -LiteralPath $env:FAKE_REGISTRY_LOG -Value "read $Name $Options"
            [IO.File]::ReadAllText($env:FAKE_USER_PATH)
        }
        $key
    }
    function global:Set-ItemProperty([string]$LiteralPath, [string]$Name, [string]$Type,
                                     [string]$Value) {
        if ($env:FAKE_PATH_DENIED) { throw 'Requested registry access is not allowed.' }
        Add-Content -LiteralPath $env:FAKE_REGISTRY_LOG -Value "write $LiteralPath $Name $Type"
        [IO.File]::WriteAllText($env:FAKE_USER_PATH, $Value)
    }
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
            executable = STEM + (".exe" if target.endswith("-windows-msvc") else "")
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
        if target.endswith("-windows-msvc"):
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
        suffix = ".zip" if self.TARGET.endswith("-windows-msvc") else ".tar.gz"
        return f"{STEM}-{VERSION}-{self.TARGET}{suffix}"

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

    def test_refuses_a_directory_where_a_file_would_go(self) -> None:
        (self.destination / self.EXECUTABLE).mkdir(parents=True)
        result = self.install(REPO, TAG, str(self.destination))
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(f"{self.EXECUTABLE} is a directory", result.stderr)
        self.assertEqual([], list((self.destination / self.EXECUTABLE).iterdir()))
        self.assertEqual({self.EXECUTABLE}, {p.name for p in self.destination.iterdir()})

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
                          (REPO, "tool/../v1.2.3"), (REPO, "v1.2.3\nv9"),
                          (REPO, TAG, str(self.destination), "extra")):
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
        # The caller's own startup and data directories are never written.
        for name in ("ZDOTDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            self.env.pop(name, None)

    def tools(self, *omit: str) -> Path:
        """A directory of the fakes and the real tools install.sh may use, less those omitted."""
        directory = self.root / "-".join(("bin", *omit))
        directory.mkdir()
        for name, text in (("gh", FAKE_GH), ("curl", FAKE_CURL), ("uname", FAKE_UNAME),
                           ("sysctl", FAKE_SYSCTL)):
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
                                      ("Darwin", "x86_64", "x86_64-apple-darwin"),
                                      ("Darwin", "arm64", "aarch64-apple-darwin")):
            with self.subTest(os=os_name, arch=arch):
                self.TARGET = target
                destination = self.root / f"{os_name}-{arch}"
                result = self.install(REPO, TAG, str(destination), FAKE_OS=os_name, FAKE_ARCH=arch)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(target.encode(), (destination / STEM).read_bytes())
                self.assertTrue(os.access(destination / STEM, os.X_OK))

    def test_installs_the_arm64_archive_from_a_shell_under_rosetta(self) -> None:
        for translated, target in (("1", "aarch64-apple-darwin"), ("0", "x86_64-apple-darwin"),
                                   ("", "x86_64-apple-darwin")):
            with self.subTest(translated=translated):
                self.TARGET = target
                destination = self.root / f"rosetta-{translated}"
                result = self.install(REPO, TAG, str(destination), FAKE_OS="Darwin",
                                      FAKE_ARCH="x86_64", FAKE_TRANSLATED=translated)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(target.encode(), (destination / STEM).read_bytes())

    def test_takes_a_directory_named_like_an_option(self) -> None:
        # A relative directory starting '-' reaches no tool as an option; one
        # starting '--' follows the end-of-options marker.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        for arguments in ((REPO, TAG, "-dir"), ("--", REPO, TAG, "--dir")):
            with self.subTest(arguments=arguments):
                self.destination = self.root / arguments[-1]
                self.log.unlink(missing_ok=True)
                self.assert_installed(self.install(*arguments))

    def test_installs_into_the_data_directory_by_default(self) -> None:
        self.destination = self.root / "data" / STEM
        result = self.install(REPO, TAG, XDG_DATA_HOME=str(self.root / "data"))
        self.assert_installed(result)
        self.assertIn(f"Add {self.destination} to PATH", result.stdout)

    def test_refuses_a_platform_without_a_build(self) -> None:
        for os_name, arch in (("Darwin", "i386"), ("Linux", "riscv64"), ("FreeBSD", "amd64"),
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

    @unittest.skipUnless(shutil.which("shasum"), "shasum required")
    def test_checks_the_archive_with_shasum_when_sha256sum_is_missing(self) -> None:
        self.env["PATH"] = str(self.tools("sha256sum"))
        self.assert_installed(self.install(REPO, TAG, str(self.destination)))
        (self.release / self.asset).write_bytes(b"tampered")
        self.destination = self.root / "second"
        self.assert_refused(self.install(REPO, TAG, str(self.destination)),
                            "does not match SHA256SUMS")

    def modify_path(self, shell: str = "/bin/sh", *arguments: str,
                    **env: str) -> subprocess.CompletedProcess[str]:
        # Last, as the guide shows it; the read-only case puts it first.
        return self.install(REPO, TAG, *arguments, "--modify-path", SHELL=shell, **env)

    def test_changes_no_startup_file_by_default(self) -> None:
        home = self.root / "home"
        home.mkdir()
        (home / ".profile").write_text("umask 022\n", encoding="utf-8")
        result = self.install(REPO, TAG, str(self.destination), SHELL="/bin/sh")
        self.assert_installed(result)
        self.assertIn(f"Add {self.destination} to PATH to run it.", result.stdout)
        self.assertEqual({".profile": "umask 022\n"},
                         {p.name: p.read_text(encoding="utf-8") for p in home.iterdir()})

    def test_modify_path_appends_one_line_to_the_login_shells_file_once(self) -> None:
        home = self.root / "home"
        line = (f"case \":${{PATH}}:\" in *:'{self.destination}':*) ;; *) "
                f"PATH=\"${{PATH:+$PATH:}}\"'{self.destination}'; export PATH ;; esac "
                "# added by release-policy install.sh")
        fish = (f"contains -- '{self.destination}' $PATH; or set -gx PATH $PATH "
                f"'{self.destination}' # added by release-policy install.sh")
        for shell, os_name, profile, expected in (
            ("/bin/sh", "Linux", ".profile", line),
            ("/usr/bin/dash", "Linux", ".profile", line),
            ("/bin/bash", "Linux", ".bashrc", line),
            ("/bin/bash", "Darwin", ".bash_profile", line),
            ("/bin/zsh", "Darwin", ".zshrc", line),
            ("/usr/bin/fish", "Linux", ".config/fish/config.fish", fish),
        ):
            with self.subTest(shell=shell, os=os_name):
                shutil.rmtree(home, ignore_errors=True)
                (home / ".config" / "fish").mkdir(parents=True)
                path = home / profile
                path.write_text("# mine\n", encoding="utf-8")
                for run in range(2):
                    self.log.unlink(missing_ok=True)
                    shutil.rmtree(self.destination, ignore_errors=True)
                    result = self.modify_path(shell, str(self.destination), FAKE_OS=os_name,
                                              FAKE_ARCH="arm64")
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(f"# mine\n{expected}\n", path.read_text(encoding="utf-8"))
                    if run == 0:
                        self.assertIn(f"Added this line to {path}:\n{expected}\n", result.stdout)
                        self.assertIn(f"To undo, delete that line from {path}.", result.stdout)
                    else:
                        self.assertIn(f"{path} already adds {self.destination} to PATH; "
                                      "it is unchanged.", result.stdout)
                    self.assertNotIn("Add ", result.stdout)
                # Only the one file changed.
                self.assertEqual([path], [p for p in home.rglob("*") if p.is_file()])

    def test_the_added_line_puts_the_directory_on_path_once(self) -> None:
        home = self.root / "home"
        # Read twice, as a nested shell would: the directory is added only once.
        self.destination = self.root / "with space+@,-" / STEM
        posix = 'PATH=$1; . "$2"; . "$2"; printf %s "$PATH"'
        for shell, options, profile, command in (
            ("sh", (), ".profile", posix),
            ("dash", (), ".profile", posix),
            ("bash", (), ".bashrc", posix),
            ("zsh", ("-f",), ".zshrc", posix),
            ("fish", ("--no-config",), ".config/fish/config.fish",
             "set -gx PATH (string split --no-empty : -- $argv[1]); source $argv[2]; "
             "source $argv[2]; string join : -- $PATH"),
        ):
            with self.subTest(shell=shell):
                found = shutil.which(shell)
                if not found:
                    self.skipTest(f"{shell} is not installed")
                shutil.rmtree(home, ignore_errors=True)
                (home / ".config" / "fish").mkdir(parents=True)
                result = self.modify_path(f"/bin/{shell}", str(self.destination))
                self.assertEqual(0, result.returncode, result.stderr)
                for start, expected in (("/usr/bin", f"/usr/bin:{self.destination}"),
                                        ("", str(self.destination))):
                    # fish takes the arguments after the command as $argv;
                    # the others take the first as $0.
                    arguments = (start, str(home / profile))
                    if shell != "fish":
                        arguments = (shell, *arguments)
                    read = subprocess.run(  # nosec B603  # nosemgrep
                        [found, *options, "-c", command, *arguments], env={"HOME": str(home)},
                        check=False, capture_output=True, text=True)
                    self.assertEqual(expected, read.stdout.rstrip("\n"), read.stderr)

    def test_modify_path_honours_zdotdir_and_xdg_config_home(self) -> None:
        home = self.root / "home"
        for shell, variable, profile in (("/bin/zsh", "ZDOTDIR", ".zshrc"),
                                         ("/usr/bin/fish", "XDG_CONFIG_HOME", "fish/config.fish")):
            with self.subTest(shell=shell):
                shutil.rmtree(home, ignore_errors=True)
                home.mkdir()
                elsewhere = self.root / variable
                (elsewhere / "fish").mkdir(parents=True, exist_ok=True)
                result = self.modify_path(shell, str(self.destination),
                                          **{variable: str(elsewhere)})
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"Added this line to {elsewhere / profile}:", result.stdout)
                self.assertEqual([], list(home.iterdir()))

    @unittest.skipUnless(shutil.which("zsh"), "zsh required")
    def test_modify_path_finds_a_zdotdir_that_zshenv_sets_without_exporting(self) -> None:
        home = self.root / "home"
        (home / "zsh").mkdir(parents=True)
        (home / ".zshenv").write_text('ZDOTDIR="$HOME/zsh"\n', encoding="utf-8")
        result = self.modify_path(str(shutil.which("zsh")), str(self.destination))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"Added this line to {home / 'zsh' / '.zshrc'}:", result.stdout)
        self.assertFalse((home / ".zshrc").exists())

    def test_modify_path_ends_a_last_line_that_lacks_a_newline(self) -> None:
        home = self.root / "home"
        home.mkdir()
        (home / ".profile").write_text("umask 022", encoding="utf-8")
        result = self.modify_path("/bin/sh", str(self.destination))
        self.assertEqual(0, result.returncode, result.stderr)
        lines = (home / ".profile").read_text(encoding="utf-8").split("\n")
        self.assertEqual(["umask 022", ""], [lines[0], lines[2]])
        self.assertTrue(lines[1].endswith("# added by release-policy install.sh"), lines)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root may write a read-only file")
    def test_modify_path_refuses_a_read_only_file_before_installing(self) -> None:
        home = self.root / "home"
        home.mkdir()
        profile = home / ".profile"
        profile.write_text("umask 022\n", encoding="utf-8")
        profile.chmod(0o444)
        result = self.install("--modify-path", REPO, TAG, str(self.destination), SHELL="/bin/sh")
        self.assert_refused(result, f"--modify-path cannot write to {profile}; nothing was installed")
        self.assertFalse(self.log.exists(), "a refused install reached the network")
        self.assertEqual("umask 022\n", profile.read_text(encoding="utf-8"))

    def test_modify_path_refuses_a_startup_file_that_is_a_symbolic_link(self) -> None:
        home = self.root / "home"
        elsewhere = self.root / "dotfiles"
        elsewhere.mkdir()
        (elsewhere / "profile").write_text("umask 022\n", encoding="utf-8")
        # A link to a writable file, and one to a file that does not exist yet.
        for target in ("profile", "missing"):
            with self.subTest(target=target):
                shutil.rmtree(home, ignore_errors=True)
                home.mkdir()
                (home / ".profile").symlink_to(elsewhere / target)
                result = self.modify_path("/bin/sh", str(self.destination))
                self.assert_refused(result, f"--modify-path will not write through the symbolic "
                                            f"link {home / '.profile'}; nothing was installed")
                self.assertFalse(self.log.exists(), "a refused install reached the network")
                self.assertEqual({"profile": "umask 022\n"},
                                 {p.name: p.read_text(encoding="utf-8")
                                  for p in elsewhere.iterdir()})

    def test_modify_path_refuses_a_directory_it_cannot_quote(self) -> None:
        home = self.root / "home"
        home.mkdir()
        for directory, reason in ((str(self.root / "it's"), "cannot add"),
                                  (str(self.root / "a$b"), "cannot add"),
                                  ("relative/dir", "needs an absolute directory")):
            with self.subTest(directory=directory):
                self.destination = Path(directory)
                result = self.modify_path("/bin/sh", directory)
                self.assert_refused(result, reason)
                self.assertEqual([], list(home.iterdir()))

    def test_refuses_an_unknown_option(self) -> None:
        result = self.install("--modify_path", REPO, TAG, str(self.destination))
        self.assert_refused(result, "unknown option --modify_path")
        self.assertFalse(self.log.exists())

    def test_refuses_without_each_required_tool(self) -> None:
        for tools, reason in ((("gh",), "the GitHub CLI (gh) is required"),
                              (("curl",), "curl is required"),
                              (("sha256sum", "shasum"), "sha256sum or shasum is required")):
            with self.subTest(tools=tools):
                result = self.install(REPO, TAG, str(self.destination), PATH=str(self.tools(*tools)))
                self.assert_refused(result, reason)


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 required")
class PowerShellInstallerTests(InstallerCases, unittest.TestCase):
    """install.ps1 as Windows sees it, x86_64 unless a test says otherwise, with PATH empty
    so only the fakes run."""

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
        quoted = " ".join(argument if argument == "-ModifyPath" else
                          "'" + argument.replace("'", "''") + "'" for argument in arguments)
        script = str(ROOT / "scripts" / "install.ps1").replace("'", "''")
        return subprocess.run(  # nosec B603  # nosemgrep
            [str(shutil.which("pwsh")), "-NoProfile", "-NonInteractive", "-Command",
             # Print a refusal's message whole: pwsh wraps its default error
             # view to the console width.
             f"{FAKE_POWERSHELL}\ntry {{ & '{script}' {quoted} }} "
             "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"],
            env={**self.env, **env}, check=False, capture_output=True, text=True)

    def test_installs_into_local_programs_by_default(self) -> None:
        self.destination = self.root / "local" / "Programs" / STEM
        result = self.install(REPO, TAG)
        self.assert_installed(result)
        self.assertIn("to PATH", result.stdout)

    def test_installs_the_arm64_archive_on_windows_on_arm(self) -> None:
        self.TARGET = WINDOWS_ARM64
        for env in ({"PROCESSOR_ARCHITECTURE": "ARM64"},
                    {"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "ARM64"}):
            with self.subTest(env=env):
                shutil.rmtree(self.destination, ignore_errors=True)
                self.log.unlink(missing_ok=True)
                self.assert_installed(self.install(REPO, TAG, str(self.destination), **env))

    def test_refuses_a_platform_without_a_build(self) -> None:
        for env, reason in (({"PROCESSOR_ARCHITECTURE": "x86"}, "no build for Windows x86"),
                            ({"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "IA64"},
                             "no build for Windows IA64"),
                            ({"OS": "Linux"}, "use install.sh on Linux and macOS")):
            with self.subTest(env=env):
                self.assert_refused(self.install(REPO, TAG, str(self.destination), **env), reason)
                self.assertFalse(self.log.exists())

    def user_path(self, value: str) -> dict[str, str]:
        """Environment that keeps the user's Path in a file holding `value`."""
        stored = self.root / "user-path"
        stored.write_text(value, encoding="utf-8")
        return {"FAKE_USER_PATH": str(stored), "FAKE_REGISTRY_LOG": str(self.root / "registry")}

    def stored_path(self) -> str:
        return (self.root / "user-path").read_text(encoding="utf-8")

    def test_changes_no_path_by_default(self) -> None:
        env = self.user_path("C:\\Windows")
        result = self.install(REPO, TAG, str(self.destination), **env)
        self.assert_installed(result)
        self.assertIn(f"Add {self.destination} to PATH to run it.", result.stdout)
        self.assertEqual("C:\\Windows", self.stored_path())
        self.assertFalse((self.root / "registry").exists())

    def test_modify_path_adds_the_directory_to_the_user_path_once(self) -> None:
        start = "%USERPROFILE%\\bin;C:\\Tools"
        env = self.user_path(start)
        for run in range(2):
            self.log.unlink(missing_ok=True)
            result = self.install(REPO, TAG, str(self.destination), "-ModifyPath", **env)
            self.assert_installed(result)
            # Entries stay as written, %USERPROFILE% unexpanded, and the
            # value keeps its expandable type.
            self.assertEqual(f"{start};{self.destination}", self.stored_path())
            if run == 0:
                self.assertIn(f"Added {self.destination} to the end of your user Path "
                              "(HKCU:\\Environment, value Path).", result.stdout)
                self.assertIn(f"To undo, remove {self.destination} from Path", result.stdout)
            else:
                self.assertIn(f"Your user Path already holds {self.destination}; it is unchanged.",
                              result.stdout)
            self.assertNotIn("to PATH to run it", result.stdout)
        self.assertEqual(["read Path DoNotExpandEnvironmentNames",
                          "write HKCU:\\Environment Path ExpandString",
                          "read Path DoNotExpandEnvironmentNames"],
                         (self.root / "registry").read_text(encoding="utf-8").splitlines())

    def test_modify_path_appends_to_the_value_as_written(self) -> None:
        # Empty entries and a trailing separator stay; only the new entry is added.
        for start, expected in (("C:\\A;;C:\\B;", f"C:\\A;;C:\\B;{self.destination}"),
                                ("", str(self.destination))):
            with self.subTest(start=start):
                env = self.user_path(start)
                self.log.unlink(missing_ok=True)
                self.assert_installed(self.install(REPO, TAG, str(self.destination),
                                                   "-ModifyPath", **env))
                self.assertEqual(expected, self.stored_path())

    def test_modify_path_adds_the_default_directory(self) -> None:
        self.destination = self.root / "local" / "Programs" / STEM
        env = self.user_path("C:\\Tools")
        self.assert_installed(self.install(REPO, TAG, "-ModifyPath", **env))
        self.assertEqual(f"C:\\Tools;{self.destination}", self.stored_path())

    def test_modify_path_knows_the_directory_through_a_variable(self) -> None:
        env = self.user_path("%RP_TOOLS%\\")
        result = self.install(REPO, TAG, str(self.destination), "-ModifyPath",
                              RP_TOOLS=str(self.destination), **env)
        self.assert_installed(result)
        self.assertIn("already holds", result.stdout)
        self.assertEqual("%RP_TOOLS%\\", self.stored_path())

    def test_modify_path_reports_a_refused_change_and_leaves_the_path(self) -> None:
        env = self.user_path("C:\\Windows")
        result = self.install(REPO, TAG, str(self.destination), "-ModifyPath",
                              FAKE_PATH_DENIED="1", **env)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(f"installed into {self.destination}, but could not change your user Path, "
                      "which is unchanged: Requested registry access is not allowed.",
                      result.stderr)
        self.assertEqual("C:\\Windows", self.stored_path())

    def test_modify_path_refuses_a_directory_holding_a_separator(self) -> None:
        env = self.user_path("C:\\Windows")
        self.destination = self.root / "a;b"
        result = self.install(REPO, TAG, str(self.destination), "-ModifyPath", **env)
        self.assert_refused(result, "-ModifyPath cannot add a directory holding ';' to Path")
        self.assertFalse(self.log.exists())
        self.assertEqual("C:\\Windows", self.stored_path())

    def test_modify_path_refuses_a_directory_holding_a_percent_sign(self) -> None:
        # Windows would read the entry as %TEMP%, the variable, not the folder.
        env = self.user_path("C:\\Windows")
        self.destination = self.root / "%TEMP%"
        result = self.install(REPO, TAG, str(self.destination), "-ModifyPath", **env)
        self.assert_refused(result, "-ModifyPath cannot add a directory holding '%' to Path")
        self.assertFalse(self.log.exists())
        self.assertEqual("C:\\Windows", self.stored_path())

    def test_refuses_when_gh_leaves_no_exit_code(self) -> None:
        result = self.install(REPO, TAG, str(self.destination), FAKE_GH="silent")
        self.assert_refused(result, "SHA256SUMS failed provenance verification")

    def test_refuses_without_the_github_cli(self) -> None:
        result = self.install(REPO, TAG, str(self.destination), FAKE_GH="0")
        self.assert_refused(result, "the GitHub CLI (gh) is required")
