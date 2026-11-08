# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""Packaging, archive validation and checks for the binary release family.

The build jobs package each target and the publisher validates what it
receives, so these checks build archives from fabricated executables and
documents and need no Rust toolchain.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import runpy
import shutil
import struct
import subprocess  # nosec B404: runs llvm-readobj on crafted headers when it is installed
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import binary_release  # noqa: E402

LINUX = "x86_64-unknown-linux-musl"
LINUX_ARM64 = "aarch64-unknown-linux-musl"
MACOS_ARM64 = "aarch64-apple-darwin"
WINDOWS = "x86_64-pc-windows-msvc"
EPOCH = 1_790_000_000
PACKAGE = '[package]\nname = "example"\nversion = "1.2.3"\npublish = false\n'
# The header lines llvm-readobj prints for each target's executable.
ELF = "File: example\nFormat: elf64-x86-64\nArch: x86_64\nAddressSize: 64bit\n"
ELF_ARM64 = "File: example\nFormat: elf64-littleaarch64\nArch: aarch64\nAddressSize: 64bit\n"
COFF = "File: example.exe\nFormat: COFF-x86-64\nArch: x86_64\nAddressSize: 64bit\n"
# What llvm-readobj 1.99.1 printed for a crafted arm64 Mach-O executable that
# loads 2 system libraries and 2 system frameworks (see macho_header below).
MACHO = """File: example
Format: Mach-O arm64
Arch: aarch64
AddressSize: 64bit
MachHeader {
  Magic: Magic64 (0xFEEDFACF)
  CpuType: Arm64 (0x100000C)
  CpuSubType: CPU_SUBTYPE_ARM64_ALL (0x0)
  FileType: Executable (0x2)
  NumOfLoadCommands: 5
  SizeOfLoadCommands: 344
  Flags [ (0x200085)
    MH_DYLDLINK (0x4)
    MH_NOUNDEFS (0x1)
    MH_PIE (0x200000)
    MH_TWOLEVEL (0x80)
  ]
  Reserved: 0x0
}
NeededLibraries [
  /System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation
  /System/Library/Frameworks/Security.framework/Versions/A/Security
  /usr/lib/libSystem.B.dylib
  /usr/lib/libiconv.2.dylib
]
"""
MACOS_SYSTEM = (
    "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
    "/System/Library/Frameworks/Security.framework/Versions/A/Security",
    "/usr/lib/libSystem.B.dylib",
    "/usr/lib/libiconv.2.dylib",
)


ARM64, X86_64 = 0x0100000C, 0x01000007
# Each load command that makes dyld load a library, by its <mach-o/loader.h> name.
DYLIB_COMMANDS = {
    "LC_LOAD_DYLIB": 0xC, "LC_LOAD_WEAK_DYLIB": 0x80000018, "LC_REEXPORT_DYLIB": 0x8000001F,
    "LC_LAZY_LOAD_DYLIB": 0x20, "LC_LOAD_UPWARD_DYLIB": 0x80000023,
}


def load_command(kind: int, name: str) -> bytes:
    """A load command naming a path: a dylib command, else a dylinker-style one."""
    fields = (24, 2, 0x10000, 0x10000) if kind in DYLIB_COMMANDS.values() else (12,)
    offset = 8 + 4 * len(fields)
    raw = name.encode() + b"\0"
    size = (offset + len(raw) + 7) // 8 * 8
    return struct.pack(f"<{2 + len(fields)}I", kind, size, *fields) + raw.ljust(size - offset, b"\0")


def macho_header(filetype: int, *dylibs: str, cputype: int = ARM64,
                 extra: tuple[bytes, ...] = ()) -> bytes:
    """A thin Mach-O header with dyld, an LC_LOAD_DYLIB for each dylib, then `extra`."""
    commands = [load_command(0xE, "/usr/lib/dyld"), *(load_command(0xC, d) for d in dylibs), *extra]
    body = b"".join(commands)
    return struct.pack("<IiiIIIII", 0xFEEDFACF, cputype, 0, filetype, len(commands),
                       len(body), 0x200085, 0) + body


def universal(*slices: tuple[int, bytes]) -> bytes:
    """A universal (fat) file holding each (cputype, thin Mach-O) slice, 4 KiB aligned."""
    arches, body = b"", b""
    for cputype, data in slices:
        arches += struct.pack(">iiIII", cputype, 0, 4096 + len(body), len(data), 12)
        body += data.ljust((len(data) + 4095) // 4096 * 4096, b"\0")
    return (struct.pack(">II", 0xCAFEBABE, len(slices)) + arches).ljust(4096, b"\0") + body


def llvm_readobj() -> Path | None:
    """The llvm-readobj of the installed Rust toolchain, when it has llvm-tools."""
    if shutil.which("rustc") is None:
        return None
    rustc = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=False)  # nosec B603 B607
    host = next((line[6:].strip() for line in rustc.stdout.splitlines() if line.startswith("host: ")), "")
    sysroot = subprocess.run(["rustc", "--print", "sysroot"], capture_output=True, text=True,  # nosec B603 B607
                             check=False).stdout.strip()
    for name in ("llvm-readobj", "llvm-readobj.exe"):
        path = Path(sysroot) / "lib" / "rustlib" / host / "bin" / name
        if path.is_file():
            return path
    return None


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "repo"
        self.source = self.root / "tools" / "example"
        self.source.mkdir(parents=True)
        (self.root / "LICENSE").write_bytes(b"licence\n")
        (self.source / "README.md").write_bytes(b"# example\n")
        self.executable = self.base / "example"
        self.executable.write_bytes(b"\x7fELF fabricated")
        (self.base / "example.exe").write_bytes(b"\x7fELF fabricated")

    def build(self, triple: str, output: str = "dist", binary: str = "example",
              data_files: str = "") -> Path:
        return binary_release.package(
            executables=self.base, binary=binary, triple=triple, stem="example",
            version="1.2.3", source=self.source, root=self.root, epoch=EPOCH,
            output_dir=self.base / output, data_files=data_files,
        )


class PackageTests(Fixture):
    def test_linux_archive_is_flat_deterministic_and_executable(self) -> None:
        first = self.build(LINUX)
        second = self.build(LINUX, "again")
        self.assertEqual(first.name, "example-1.2.3-x86_64-unknown-linux-musl.tar.gz")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first, mode="r:gz") as archive:
            members = [(m.name, m.mode, m.mtime, m.uid, m.uname) for m in archive.getmembers()]
        self.assertEqual(members, [
            ("example", 0o755, EPOCH, 0, ""),
            ("LICENSE", 0o644, EPOCH, 0, ""),
            ("README.md", 0o644, EPOCH, 0, ""),
        ])
        self.assertEqual(gzip.open(first).read()[:7], b"example")

    def test_windows_archive_names_the_exe_and_a_component_notice_wins(self) -> None:
        (self.root / "NOTICE").write_bytes(b"root notice\n")
        (self.source / "NOTICE").write_bytes(b"component notice\n")
        first = self.build(WINDOWS)
        self.assertEqual(first.read_bytes(), self.build(WINDOWS, "again").read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(archive.namelist(), ["example.exe", "LICENSE", "NOTICE", "README.md"])
            self.assertEqual(archive.read("NOTICE"), b"component notice\n")
            self.assertEqual(archive.getinfo("example.exe").external_attr >> 16, 0o100755)

    def test_an_early_epoch_is_clamped_to_the_zip_minimum(self) -> None:
        archive = binary_release.package(
            executables=self.base, binary="example", triple=WINDOWS, stem="example",
            version="1.2.3", source=self.source, root=self.root, epoch=0,
            output_dir=self.base / "dist",
        )
        with zipfile.ZipFile(archive) as opened:
            self.assertEqual(opened.getinfo("LICENSE").date_time, (1980, 1, 1, 0, 0, 0))

    def test_missing_documents_existing_outputs_and_bad_names_are_refused(self) -> None:
        self.build(LINUX)
        with self.assertRaises(FileExistsError):
            self.build(LINUX)
        (self.root / "LICENSE").unlink()
        with self.assertRaisesRegex(ValueError, "missing release documents: LICENSE"):
            self.build(WINDOWS)
        for stem, version, triple, message in (
            ("Example", "1.2.3", LINUX, "artifact-stem"),
            ("example", "1.2", LINUX, "version"),
            ("example", "1.2.3", "x86_64-apple-darwin", "unsupported target"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                binary_release.asset_name(stem, version, triple)
        with self.assertRaisesRegex(ValueError, "binary is not valid"):
            binary_release.member_names("../x", LINUX, ())

    def test_each_unix_target_ships_an_executable_tar_archive(self) -> None:
        for triple in (LINUX_ARM64, MACOS_ARM64):
            with self.subTest(triple=triple):
                archive = self.build(triple, triple)
                self.assertEqual(archive.name, f"example-1.2.3-{triple}.tar.gz")
                with tarfile.open(archive, mode="r:gz") as opened:
                    self.assertEqual([(m.name, m.mode) for m in opened.getmembers()], [
                        ("example", 0o755), ("LICENSE", 0o644), ("README.md", 0o644)])

    def test_several_executables_and_declared_data_files_ship_together(self) -> None:
        (self.base / "example-helper").write_bytes(b"helper")
        (self.source / "certs").mkdir()
        (self.source / "certs" / "ca-bundle.pem").write_bytes(b"certificates\n")
        archive = self.build(LINUX, binary="example example-helper",
                             data_files="\n  certs/ca-bundle.pem\n\n")
        with tarfile.open(archive, mode="r:gz") as opened:
            self.assertEqual([(m.name, m.mode) for m in opened.getmembers()], [
                ("example", 0o755), ("example-helper", 0o755), ("LICENSE", 0o644),
                ("README.md", 0o644), ("ca-bundle.pem", 0o644)])
            self.assertEqual(member_bytes(opened, "ca-bundle.pem"), b"certificates\n")

    def test_bad_binary_lists_and_data_files_are_refused(self) -> None:
        for binary, message in (("", "one or more distinct"), ("a a", "one or more distinct"),
                                ("a b/c", "binary is not valid")):
            with self.subTest(binary=binary), self.assertRaisesRegex(ValueError, message):
                binary_release.binaries(binary)
        for listing, message in (
            ("../ca.pem", "data file is not valid"), ("/etc/ca.pem", "data file is not valid"),
            ("certs/../ca.pem", "data file is not valid"), (".env", "data file is not valid"),
            ("certs\\ca.pem", "data file is not valid"),
            ("a/ca.pem\nb/ca.pem", "would ship as ca.pem, a name already taken"),
            ("docs/LICENSE", "would ship as LICENSE"), ("NOTICE", "would ship as NOTICE"),
        ):
            with self.subTest(listing=listing), self.assertRaisesRegex(ValueError, message):
                binary_release.data_names(listing)
        with self.assertRaisesRegex(ValueError, "cannot hold"):
            binary_release.member_names("example", LINUX, ("LICENSE",), ("example",))
        with self.assertRaisesRegex(ValueError, "at most 12 distinct members"):
            binary_release.member_names(" ".join(f"tool{n}" for n in range(11)), LINUX,
                                        ("LICENSE", "README.md"))
        (self.base / "outside.pem").write_bytes(b"x")
        (self.source / "certs").mkdir()
        for entry, make in (
            ("certs/missing.pem", None),
            ("certs", None),
            ("linked.pem", lambda path: path.symlink_to(self.base / "outside.pem")),
            ("through/outside.pem", lambda path: path.parent.symlink_to(self.base,
                                                                         target_is_directory=True)),
        ):
            path = self.source / entry
            if make is not None:
                try:
                    make(path)
                except OSError:
                    continue  # this platform refuses unprivileged links
            with self.subTest(entry=entry), self.assertRaisesRegex(
                    ValueError, f"data file {entry} is not a regular file"):
                binary_release.find_data_files(self.source, entry)


class VerifyTests(Fixture):
    def tar(self, members: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
        path = self.base / "crafted.tar.gz"
        with tarfile.open(path, mode="w:gz") as archive:
            for info, data in members:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    def info(self, name: str, mode: int = 0o644, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.mode, info.type = mode, kind
        return info

    def test_a_packaged_archive_verifies_and_extracts_exactly_its_members(self) -> None:
        for triple, executable in ((LINUX, "example"), (WINDOWS, "example.exe")):
            with self.subTest(triple=triple):
                archive = self.build(triple, triple)
                out = self.base / f"out-{triple}"
                names = binary_release.verify(archive=archive, binary="example", triple=triple,
                                              extract_dir=out)
                self.assertEqual(names, (executable, "LICENSE", "README.md"))
                self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(names))
                self.assertEqual((out / executable).read_bytes(), self.executable.read_bytes())

    def test_unexpected_unsafe_linked_or_duplicate_members_are_refused(self) -> None:
        exe = (self.info("example", 0o755), b"x")
        docs = [(self.info("LICENSE"), b"l"), (self.info("README.md"), b"r")]
        cases = {
            "expected": [exe, *docs, (self.info("extra.txt"), b"e")],
            "unsafe member name": [exe, *docs, (self.info("../LICENSE"), b"l")],
            "not a regular file": [exe, *docs, (self.info("NOTICE", kind=tarfile.SYMTYPE), b"")],
            "duplicate member": [exe, *docs, docs[0]],
            "is missing": [(self.info("other", 0o755), b"x"), *docs],
            "not executable": [(self.info("example"), b"x"), *docs],
        }
        for message, members in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                binary_release.verify(archive=self.tar(members), binary="example", triple=LINUX,
                                      extract_dir=None)
        missing_readme = [exe, docs[0]]
        with self.assertRaisesRegex(ValueError, "expected"):
            binary_release.verify(archive=self.tar(missing_readme), binary="example",
                                  triple=LINUX, extract_dir=None)

    def test_every_named_executable_and_data_file_must_ship(self) -> None:
        helper = (self.info("example-helper", 0o755), b"h")
        exe = (self.info("example", 0o755), b"x")
        docs = [(self.info("LICENSE"), b"l"), (self.info("README.md"), b"r")]
        pem = (self.info("ca.pem"), b"c")
        both, data = "example example-helper", "certs/ca.pem"
        self.assertEqual(
            binary_release.verify(archive=self.tar([exe, helper, *docs, pem]), binary=both,
                                  triple=MACOS_ARM64, extract_dir=None, data_files=data),
            ("example", "example-helper", "LICENSE", "README.md", "ca.pem"))
        for message, members, binary, listing in (
            ("the executable example-helper is missing", [exe, *docs, pem], both, data),
            ("the data file ca.pem is missing", [exe, helper, *docs], both, data),
            ("expected", [exe, helper, *docs, pem], both, ""),
            ("example-helper is not executable",
             [exe, (self.info("example-helper"), b"h"), *docs, pem], both, data),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                binary_release.verify(archive=self.tar(members), binary=binary,
                                      triple=MACOS_ARM64, extract_dir=None, data_files=listing)

    def test_too_many_or_oversized_members_are_refused_before_reading(self) -> None:
        exe = (self.info("example", 0o755), b"x")
        docs = [(self.info(name), b"d") for name in ("LICENSE", "NOTICE", "README.md")]
        extra = [(self.info(f"extra{n}"), b"e") for n in range(binary_release.MAX_MEMBERS - 3)]
        with self.assertRaisesRegex(ValueError, "extra8 exceeds the archive's member count"):
            binary_release.verify(archive=self.tar([exe, *docs, *extra]), binary="example",
                                  triple=LINUX, extract_dir=None)
        with mock.patch.object(binary_release, "MAX_MEMBER_BYTES", 0):
            with self.assertRaisesRegex(ValueError, "example exceeds"):
                binary_release.verify(archive=self.tar([exe]), binary="example", triple=LINUX,
                                      extract_dir=None)

    def test_oversized_archives_and_extension_metadata_are_refused_before_parsing(self) -> None:
        exe = (self.info("example", 0o755), b"x")
        docs = [(self.info("LICENSE"), b"l"), (self.info("README.md"), b"r")]
        plain = self.tar([exe, *docs])
        with mock.patch.object(binary_release, "MAX_ARCHIVE_BYTES", 0):
            with self.assertRaisesRegex(ValueError, "exceeds the archive size limit"):
                binary_release.verify(archive=plain, binary="example", triple=LINUX,
                                      extract_dir=None)
        # A header alone passes 512 decompressed bytes, so the stream stops early.
        with mock.patch.object(binary_release, "MAX_DECOMPRESSED_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "decompresses beyond the size limit"):
                binary_release.verify(archive=plain, binary="example", triple=LINUX,
                                      extract_dir=None)
        for name, kwargs in (("member", {}), ("global", {"pax_headers": {"comment": "x"}})):
            path = self.base / f"pax-{name}.tar.gz"
            with tarfile.open(path, mode="w:gz", format=tarfile.PAX_FORMAT, **kwargs) as archive:
                for info, data in [exe, *docs]:
                    info.size = len(data)
                    if name == "member":
                        info.pax_headers = {"comment": "x"}
                    archive.addfile(info, io.BytesIO(data))
            with self.subTest(pax=name), self.assertRaisesRegex(ValueError, "uses PAX headers"):
                binary_release.verify(archive=path, binary="example", triple=LINUX,
                                      extract_dir=None)

    def test_a_zip_directory_is_bounded_before_zipfile_reads_it(self) -> None:
        path = self.base / "many.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name in ("example.exe", "LICENSE", "README.md", "NOTICE",
                         *(f"extra{n}" for n in range(binary_release.MAX_MEMBERS - 3))):
                archive.writestr(name, b"x")
        with self.assertRaisesRegex(ValueError, "lists 13 members"):
            binary_release.verify(archive=path, binary="example", triple=WINDOWS,
                                  extract_dir=None)
        archive = self.build(WINDOWS)
        with mock.patch.object(binary_release, "_MAX_ZIP_DIRECTORY_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "-byte directory"):
                binary_release.verify(archive=archive, binary="example", triple=WINDOWS,
                                      extract_dir=None)
        data = archive.read_bytes()
        zip64 = self.base / "zip64.zip"
        zip64.write_bytes(data[:-22] + b"PK\x06\x07" + bytes(16) + data[-22:])
        with self.assertRaisesRegex(ValueError, "is a ZIP64 archive"):
            binary_release.verify(archive=zip64, binary="example", triple=WINDOWS,
                                  extract_dir=None)
        # A maximum-length comment puts the record at the start of a
        # 65,557-byte search window, and its locator just before it.
        commented = self.base / "commented.zip"
        with zipfile.ZipFile(commented, "w") as writer:
            writer.writestr("example.exe", b"x")
            writer.comment = b"c" * 0xFFFF
        data = commented.read_bytes()
        record = len(data) - 22 - 0xFFFF
        commented.write_bytes(data[:record] + b"PK\x06\x07" + bytes(16) + data[record:])
        with self.assertRaisesRegex(ValueError, "is a ZIP64 archive"):
            binary_release.verify(archive=commented, binary="example", triple=WINDOWS,
                                  extract_dir=None)
        truncated = self.base / "truncated.zip"
        truncated.write_bytes(archive.read_bytes()[:-10])
        with self.assertRaisesRegex(ValueError, "no ZIP end of central directory"):
            binary_release.verify(archive=truncated, binary="example", triple=WINDOWS,
                                  extract_dir=None)

    def test_zip_directories_links_and_duplicates_are_refused(self) -> None:
        def crafted(entries: list[tuple[str, int]]) -> Path:
            path = self.base / "crafted.zip"
            path.unlink(missing_ok=True)
            with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
                warnings.simplefilter("ignore")  # the duplicate case is deliberate
                for name, mode in entries:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode << 16
                    archive.writestr(info, b"x")
            return path

        regular = [("example.exe", 0o100755), ("LICENSE", 0), ("README.md", 0o100644)]
        for message, extra in (
            ("not a regular file", ("NOTICE", 0o120777)),
            ("not a regular file", ("docs/", 0o040755)),
            ("duplicate member", ("LICENSE", 0o100644)),
        ):
            archive = crafted([*regular, extra])
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                binary_release.verify(archive=archive, binary="example", triple=WINDOWS,
                                      extract_dir=None)
        self.assertEqual(
            binary_release.verify(archive=crafted(regular), binary="example", triple=WINDOWS,
                                  extract_dir=None),
            ("example.exe", "LICENSE", "README.md"),
        )


class CargoTests(Fixture):
    def manifest(self, text: str | bytes) -> Path:
        path = self.base / "Cargo.toml"
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        return path

    def test_the_package_must_agree_with_the_release(self) -> None:
        path = self.manifest(PACKAGE + '[[bin]]\nname = "example-tool"\n')
        self.assertEqual(binary_release.cargo_package(path, "1.2.3", "example"), "example")
        self.assertEqual(binary_release.cargo_package(path, "1.2.3", "example-tool"), "example")
        with self.assertRaisesRegex(ValueError, "version 1.2.3 != release version 1.2.4"):
            binary_release.cargo_package(path, "1.2.4", "example")
        with self.assertRaisesRegex(ValueError, "neither the package example nor a declared"):
            binary_release.cargo_package(path, "1.2.3", "other")
        malformed_bins = self.manifest('bin = "x"\n' + PACKAGE)
        with self.assertRaisesRegex(ValueError, "neither the package"):
            binary_release.cargo_package(malformed_bins, "1.2.3", "other")

    def test_inherited_missing_malformed_or_linked_metadata_is_refused(self) -> None:
        for text, message in (
            ('[package]\nname = "example"\nversion.workspace = true\n', "workspace inheritance"),
            ('[workspace]\nmembers = []\n', r"no \[package\] table"),
            ("[package\n", "not a readable Cargo manifest"),
            (b"\xff", "not a readable Cargo manifest"),
            ('[package]\nname = "example"\nversion = "1.2.3-rc.1"\n', "package version"),
            ('[package]\nname = "example"\nversion = "1.2.3"\n', "publish = false"),
            ('[package]\nname = "example"\nversion = "1.2.3"\npublish = ["crates-io"]\n',
             "publish = false"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                binary_release.cargo_package(self.manifest(text), "1.2.3", "example")
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                binary_release.cargo_package(self.manifest(PACKAGE), "1.2.3", "example")


class ToolchainTests(Fixture):
    def test_the_nearest_toolchain_file_must_pin_an_exact_channel(self) -> None:
        (self.root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.99.1"\n',
                                                       encoding="utf-8")
        self.assertEqual(binary_release.toolchain(self.source, self.root), "1.99.1")
        (self.source / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "stable"\n',
                                                         encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exact MAJOR.MINOR.PATCH channel"):
            binary_release.toolchain(self.source, self.root)
        for text in ('channel = "1.99.1"\n', "[toolchain\n"):
            (self.source / "rust-toolchain.toml").write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "rust-toolchain.toml"):
                binary_release.toolchain(self.source, self.root)

    def test_legacy_linked_missing_or_outside_files_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no rust-toolchain.toml between"):
            binary_release.toolchain(self.source, self.root)
        with self.assertRaisesRegex(ValueError, "is not inside"):
            binary_release.toolchain(self.root, self.source)
        (self.source / "rust-toolchain.toml").mkdir()
        with self.assertRaisesRegex(ValueError, "must be a regular file"):
            binary_release.toolchain(self.source, self.root)
        (self.root / "tools" / "rust-toolchain").write_text("1.99.1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "use rust-toolchain.toml"):
            binary_release.toolchain(self.root / "tools", self.root)


class ReceiveTests(Fixture):
    def artefact(self, triple: str = LINUX, binary: str = "example",
                 data_files: str = "") -> Path:
        incoming = self.base / f"incoming-{triple}"
        archive = self.build(triple, incoming.name, binary, data_files)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (incoming / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n",
                                                         encoding="utf-8")
        return incoming

    def receive(self, incoming: Path, triple: str = LINUX, binary: str = "example",
                data_files: str = "") -> Path:
        return binary_release.receive(
            incoming=incoming, stem="example", version="1.2.3", binary=binary,
            triple=triple, dist=self.base / "dist", extract_dir=self.base / "extracted",
            source=self.source, root=self.root, data_files=data_files,
        )

    def test_a_matching_artefact_is_admitted_and_extracted(self) -> None:
        incoming = self.artefact(WINDOWS)
        admitted = self.receive(incoming, WINDOWS)
        self.assertEqual(admitted.read_bytes(),
                         (incoming / "example-1.2.3-x86_64-pc-windows-msvc.zip").read_bytes())
        self.assertTrue((self.base / "extracted" / "example.exe").is_file())
        with self.assertRaises(FileExistsError):
            binary_release.receive(
                incoming=incoming, stem="example", version="1.2.3", binary="example",
                triple=WINDOWS, dist=self.base / "dist", extract_dir=self.base / "again",
                source=self.source, root=self.root,
            )
        self.assertEqual(admitted.read_bytes(),
                         (incoming / "example-1.2.3-x86_64-pc-windows-msvc.zip").read_bytes())

    def test_documents_must_be_the_tagged_ones_and_a_refusal_keeps_nothing(self) -> None:
        admitted = self.base / "dist" / "example-1.2.3-x86_64-unknown-linux-musl.tar.gz"
        for message, change in (
            ("'s LICENSE differs from the tagged LICENSE",
             lambda: (self.root / "LICENSE").write_bytes(b"other licence\n")),
            (r"ships \['LICENSE', 'README.md'\], the tag has \['LICENSE', 'NOTICE', 'README.md'\]",
             lambda: (self.source / "NOTICE").write_bytes(b"notice\n")),
        ):
            incoming = self.artefact()
            change()
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.receive(incoming)
            self.assertFalse(admitted.exists())
            shutil.rmtree(incoming)
            shutil.rmtree(self.base / "extracted")

    def test_declared_data_files_must_be_the_tagged_ones(self) -> None:
        (self.base / "example-helper").write_bytes(b"helper")
        (self.source / "certs").mkdir()
        pem = self.source / "certs" / "ca.pem"
        pem.write_bytes(b"certificates\n")
        both, data = "example example-helper", "certs/ca.pem"
        incoming = self.artefact(MACOS_ARM64, both, data)
        self.receive(incoming, MACOS_ARM64, both, data)
        self.assertEqual((self.base / "extracted" / "ca.pem").read_bytes(), b"certificates\n")
        shutil.rmtree(self.base / "dist")
        shutil.rmtree(self.base / "extracted")
        pem.write_bytes(b"other certificates\n")
        with self.assertRaisesRegex(ValueError, "'s ca.pem differs from the tagged ca.pem"):
            self.receive(incoming, MACOS_ARM64, both, data)
        self.assertFalse((self.base / "dist" / "example-1.2.3-aarch64-apple-darwin.tar.gz").exists())

    def test_a_tampered_archive_of_each_target_is_refused(self) -> None:
        for triple in binary_release.TARGETS:
            name = binary_release.asset_name("example", "1.2.3", triple)
            with self.subTest(triple=triple):
                incoming = self.artefact(triple)
                # The build job's digest line stays; its archive is rebuilt
                # around a different executable.
                (incoming / name).unlink()
                replaced = self.base / "replaced"
                replaced.mkdir()
                for executable in ("example", "example.exe"):
                    (replaced / executable).write_bytes(b"replaced")
                binary_release.package(
                    executables=replaced, binary="example", triple=triple, stem="example",
                    version="1.2.3", source=self.source, root=self.root, epoch=EPOCH,
                    output_dir=incoming)
                shutil.rmtree(replaced)
                with self.assertRaisesRegex(ValueError, "does not match its recorded digest"):
                    self.receive(incoming, triple)
                self.assertFalse((self.base / "dist" / name).exists())
                shutil.rmtree(incoming)

    def test_extra_missing_mismatched_or_malformed_inputs_are_refused(self) -> None:
        name = "example-1.2.3-x86_64-unknown-linux-musl.tar.gz"

        def replace_with_directory(directory: Path) -> None:
            (directory / name).unlink()
            (directory / name).mkdir()

        cases = {
            "artefact holds": lambda d: (d / "extra").write_bytes(b""),
            "not one sha256sum line": lambda d: (d / f"{name}.sha256").write_text(
                f"{'0' * 64}  other.tar.gz\n", encoding="utf-8"),
            "does not match its recorded digest": lambda d: (d / f"{name}.sha256").write_text(
                f"{'0' * 64} *{name}\n", encoding="utf-8"),
            "is not a regular file": replace_with_directory,
        }
        for message, damage in cases.items():
            incoming = self.artefact()
            damage(incoming)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.receive(incoming)
            self.assertFalse((self.base / "dist" / name).exists())
            shutil.rmtree(incoming)
        incoming = self.artefact()
        (incoming / f"{name}.sha256").write_bytes(b"0" * 2048)
        with self.assertRaisesRegex(ValueError, "not one sha256sum line"):
            self.receive(incoming)
        shutil.rmtree(incoming)
        incoming = self.artefact()
        with mock.patch.object(binary_release, "MAX_ARCHIVE_BYTES", 0):
            with self.assertRaisesRegex(ValueError, "exceeds the archive size limit"):
                self.receive(incoming)
        (incoming / f"{name}.sha256").unlink()
        with self.assertRaisesRegex(ValueError, "artefact holds"):
            self.receive(incoming)


class CheckTests(Fixture):
    def test_the_version_line_must_end_in_the_release_version(self) -> None:
        def ran(code: int, stdout: str) -> mock.Mock:
            return mock.Mock(returncode=code, stdout=stdout)

        with mock.patch.object(binary_release.subprocess, "run",
                               side_effect=[ran(0, "example 1.2.3\nmore\n"), ran(0, "example 1.2.30\n"),
                                            ran(1, "example 1.2.3\n"), ran(0, "")]) as run:
            self.assertEqual(binary_release.smoke(self.executable, "1.2.3"), "example 1.2.3")
            self.assertEqual(run.call_args.args[0], [str(self.executable), "--version"])
            for _ in range(3):
                with self.assertRaisesRegex(ValueError, "expected a first line ending in ' 1.2.3'"):
                    binary_release.smoke(self.executable, "1.2.3")

    def test_a_static_musl_executable_needs_nothing(self) -> None:
        static = (ELF + "NeededLibraries [\n]\nProgramHeaders [\n  ProgramHeader {\n"
                  "    Type: PT_LOAD (0x1)\n  }\n]\n")
        self.assertEqual(binary_release.linkage(static, LINUX), ())
        for output in (static.replace("NeededLibraries [\n", "NeededLibraries [\n  libc.so\n"),
                       static.replace("PT_LOAD (0x1)", "PT_INTERP (0x3)")):
            with self.assertRaisesRegex(ValueError, "not a static executable"):
                binary_release.linkage(output, LINUX)
        with self.assertRaisesRegex(ValueError, "no NeededLibraries list"):
            binary_release.linkage(ELF, LINUX)
        for wrong in (static.replace("Arch: x86_64", "Arch: aarch64"), COFF + "NeededLibraries [\n]\n"):
            with self.assertRaisesRegex(ValueError, "not an x86_64 elf64-x86-64 executable"):
                binary_release.linkage(wrong, LINUX)
        arm64 = ELF_ARM64 + "NeededLibraries [\n]\nProgramHeaders [\n]\n"
        self.assertEqual(binary_release.linkage(arm64, LINUX_ARM64), ())
        with self.assertRaisesRegex(ValueError, "not a static executable"):
            binary_release.linkage(arm64.replace("[\n]\nProgram", "[\n  libc.so\n]\nProgram"),
                                   LINUX_ARM64)
        for wrong in (static, MACHO):
            with self.assertRaisesRegex(ValueError, "not an aarch64 elf64-littleaarch64 executable"):
                binary_release.linkage(wrong, LINUX_ARM64)
        # llvm-readobj prints a block for each slice of a universal file; one
        # clean block must not stand for the others.
        with self.assertRaisesRegex(ValueError, "describes more than one object"):
            binary_release.linkage(static + static, LINUX)

    def test_a_macos_executable_loads_only_libraries_macos_ships(self) -> None:
        self.assertEqual(binary_release.macho_linkage(macho_header(2, *MACOS_SYSTEM), MACOS_ARM64),
                         MACOS_SYSTEM)
        # A command that names no library, here LC_UUID, is passed over.
        uuid = struct.pack("<II16s", 0x1B, 24, bytes(range(16)))
        self.assertEqual(binary_release.macho_linkage(
            macho_header(2, *MACOS_SYSTEM, extra=(uuid,)), MACOS_ARM64), MACOS_SYSTEM)
        for name in ("/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib", "/usr/local/lib/libz.dylib",
                     "@rpath/libexample.dylib", "@executable_path/libexample.dylib",
                     "@loader_path/../lib/libexample.dylib", "/usr/lib/../local/lib/libz.dylib",
                     "/usr/lib/swift/libswiftCore.dylib", "libz.dylib",
                     "/System/Library/Frameworks/Security.framework/Versions/A/Other",
                     "/Library/Frameworks/Example.framework/Versions/A/Example"):
            # Every kind of command that loads a library is read, not only LC_LOAD_DYLIB.
            for command, kind in DYLIB_COMMANDS.items():
                executable = macho_header(2, "/usr/lib/libSystem.B.dylib",
                                          extra=(load_command(kind, name),))
                with self.subTest(name=name, command=command), self.assertRaisesRegex(
                        ValueError, "loads libraries macOS does not ship"):
                    binary_release.macho_linkage(executable, MACOS_ARM64)
        with self.assertRaisesRegex(ValueError, "not a Mach-O executable file type"):
            binary_release.macho_linkage(macho_header(8, *MACOS_SYSTEM), MACOS_ARM64)
        with self.assertRaisesRegex(ValueError, "not an arm64 Mach-O executable"):
            binary_release.macho_linkage(macho_header(2, cputype=X86_64), MACOS_ARM64)
        with self.assertRaisesRegex(ValueError, "read from its load commands"):
            binary_release.linkage(MACHO, MACOS_ARM64)
        with self.assertRaisesRegex(ValueError, "only a macOS executable"):
            binary_release.macho_linkage(macho_header(2), LINUX_ARM64)

    def test_a_universal_or_other_non_thin_arm64_file_is_refused(self) -> None:
        thin = macho_header(2, "/usr/lib/libSystem.B.dylib")
        # The reviewer's case: a clean x86_64 slice first, an arm64 slice loading
        # a library the release does not ship second.
        clean = macho_header(2, "/usr/lib/libSystem.B.dylib", cputype=X86_64)
        evil = macho_header(2, "/usr/lib/libSystem.B.dylib", "@rpath/libevil.dylib")
        fat = universal((X86_64, clean), (ARM64, evil))
        for name, data in (
            ("fat, clean slice first", fat),
            ("fat, one clean slice", universal((ARM64, thin))),
            ("fat64", struct.pack(">I", 0xCAFEBABF) + fat[4:]),
            ("big-endian", struct.pack(">I", 0xFEEDFACF) + thin[4:]),
            ("32-bit", struct.pack("<I", 0xFEEDFACE) + thin[4:]),
            ("ELF", b"\x7fELF" + thin[4:]),
            ("empty", b""),
        ):
            with self.subTest(name), self.assertRaisesRegex(
                    ValueError, "not a thin 64-bit little-endian Mach-O file"):
                binary_release.macho_linkage(data, MACOS_ARM64)

    def test_load_commands_that_redirect_dyld_are_refused(self) -> None:
        for command, refusal in (
            (load_command(0x27, "DYLD_LIBRARY_PATH=@executable_path/lib"),
             "sets dyld environment variables"),
            (load_command(0x8000001C, "@executable_path/../lib"), "adds a run-path search path"),
            (load_command(0xE, "/tmp/dyld"), "names a dynamic linker other than /usr/lib/dyld"),
        ):
            with self.subTest(refusal), self.assertRaisesRegex(ValueError, refusal):
                binary_release.macho_linkage(
                    macho_header(2, "/usr/lib/libSystem.B.dylib", extra=(command,)), MACOS_ARM64)

    def test_malformed_load_commands_are_refused(self) -> None:
        thin = macho_header(2, "/usr/lib/libSystem.B.dylib")
        # The dyld command fills bytes 32 to 64; the dylib command starts at 64
        # and its name, at offset 24 within it, at 88.
        for name, data in (
            ("short header", thin[:31]),
            ("commands past the end", thin[:-8]),
            ("more commands than bytes", thin[:16] + struct.pack("<I", 3) + thin[20:]),
            ("fewer commands than bytes", thin[:16] + struct.pack("<I", 1) + thin[20:]),
            ("zero command size", thin[:36] + struct.pack("<I", 0) + thin[40:]),
            ("command too short for a name", thin[:68] + struct.pack("<I", 8) + thin[72:]),
            ("name inside the header", thin[:72] + struct.pack("<I", 4) + thin[76:]),
            ("name without a terminator", thin[:88] + b"x" * 32),
            ("name not UTF-8", thin[:88] + b"\xff" + thin[89:]),
        ):
            with self.subTest(name), self.assertRaisesRegex(
                    ValueError, "malformed Mach-O load commands"):
                binary_release.macho_linkage(data, MACOS_ARM64)

    def test_llvm_readobj_reads_the_crafted_mach_o_files_alike(self) -> None:
        readobj = llvm_readobj()
        if readobj is None:
            self.skipTest("llvm-readobj from the Rust toolchain's llvm-tools is not installed")
        path = self.base / "example"

        def read(data: bytes) -> str:
            path.write_bytes(data)
            return subprocess.run(  # nosec B603
                [str(readobj), "--file-headers", "--needed-libs", str(path)],
                capture_output=True, text=True, check=True).stdout

        # The fixture above is what LLVM prints for the plain crafted file.
        output = read(macho_header(2, *MACOS_SYSTEM))
        self.assertEqual(output[output.index("Format:"):], MACHO[MACHO.index("Format:"):])
        # LLVM lists the library of every dylib command the parser reads.
        extra = tuple(load_command(kind, f"/usr/lib/lib{command}.dylib")
                      for command, kind in DYLIB_COMMANDS.items())
        executable = macho_header(2, *MACOS_SYSTEM, extra=extra)
        self.assertEqual(sorted(binary_release.needed_libraries(read(executable))),
                         sorted(binary_release.macho_linkage(executable, MACOS_ARM64)))
        # LLVM reads the universal file too, printing one block for each slice.
        clean = macho_header(2, "/usr/lib/libSystem.B.dylib", cputype=X86_64)
        evil = macho_header(2, "/usr/lib/libSystem.B.dylib", "@rpath/libevil.dylib")
        output = read(universal((X86_64, clean), (ARM64, evil)))
        self.assertEqual(output.count("Format: Mach-O"), 2)
        self.assertIn("@rpath/libevil.dylib", output)

    def test_windows_imports_only_pinned_system_dlls_and_no_c_runtime(self) -> None:
        # The imports of release-policy's own static-CRT build, 28 October 2026.
        listing = (COFF + "NeededLibraries [\r\n  KERNEL32.dll\r\n"
                   "  api-ms-win-core-synch-l1-2-0.dll\r\n  ntdll.dll\r\n]\r\n")
        self.assertEqual(binary_release.linkage(listing, WINDOWS),
                         ("KERNEL32.dll", "api-ms-win-core-synch-l1-2-0.dll", "ntdll.dll"))
        with self.assertRaisesRegex(ValueError, "C runtime DLLs: \\['VCRUNTIME140.dll'"):
            binary_release.linkage(listing.replace("ntdll", "VCRUNTIME140"), WINDOWS)
        with self.assertRaisesRegex(ValueError, "C runtime DLLs"):
            binary_release.linkage(listing.replace("ntdll", "api-ms-win-crt-heap-l1-1-0"),
                                   WINDOWS)
        with self.assertRaisesRegex(ValueError, "C runtime DLLs: \\['msvcrt.dll'"):
            binary_release.linkage(listing.replace("ntdll", "msvcrt"), WINDOWS)
        for path in ("..\\..\\Temp\\helper.dll", "C:\\Temp\\helper.dll", "sub/helper.dll"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "by path"):
                binary_release.linkage(listing.replace("ntdll.dll", path), WINDOWS)
        # A real Windows DLL outside the pinned set is refused as well.
        for name in ("example-helper", "d3d11"):
            with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, f"outside the pinned Windows system set: \\['{name}.dll'"):
                binary_release.linkage(listing.replace("ntdll", name), WINDOWS)

    def test_the_sbom_must_list_the_package(self) -> None:
        sbom = self.base / "x.spdx.json"
        for document, expected in (
            ({"packages": [{"name": "serde"}, {"name": "example"}]}, True),
            ({"packages": [{"name": "serde"}, "example"]}, False),
            ({"packages": {}}, False),
            ([], False),
        ):
            sbom.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(document=document):
                self.assertIs(binary_release.sbom_lists(sbom, "example"), expected)


class CommandTests(Fixture):
    def run_main(self, *argv: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = binary_release.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_each_command_reports_its_result_or_one_error_line(self) -> None:
        manifest = self.base / "Cargo.toml"
        manifest.write_text(PACKAGE, encoding="utf-8")
        self.assertEqual(self.run_main("cargo-package", "--manifest", str(manifest), "--version",
                                       "1.2.3", "--binary", "example"), (0, "example\n", ""))
        self.assertEqual(
            self.run_main("cargo-package", "--manifest", str(manifest), "--version", "1.2.4",
                          "--binary", "example"),
            (1, "", "binary_release: Cargo.toml version 1.2.3 != release version 1.2.4\n"),
        )
        (self.root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.99.1"\n',
                                                       encoding="utf-8")
        self.assertEqual(self.run_main("toolchain", "--source", str(self.source), "--root",
                                       str(self.root)), (0, "1.99.1\n", ""))
        package = ("package", "--binary", "example", "--target", LINUX, "--stem", "example",
                   "--version", "1.2.3", "--executables", str(self.base), "--source",
                   str(self.source), "--root", str(self.root), "--output-dir",
                   str(self.base / "incoming"), "--epoch")
        self.assertEqual(self.run_main(*package, "soon"),
                         (1, "", "binary_release: epoch is not a Unix time: 'soon'\n"))
        code, out, _ = self.run_main(*package, str(EPOCH))
        archive = Path(out.strip())
        self.assertEqual((code, archive.name), (0, "example-1.2.3-x86_64-unknown-linux-musl.tar.gz"))
        self.assertEqual(
            self.run_main("verify", "--archive", str(archive), "--binary", "example", "--target",
                          LINUX),
            (0, "example\nLICENSE\nREADME.md\n", ""),
        )
        self.assertEqual(
            self.run_main("verify", "--archive", str(archive), "--binary", "example", "--target",
                          LINUX, "--data-files", "certs/ca.pem"),
            (1, "", "binary_release: the data file ca.pem is missing\n"),
        )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        code, out, _ = self.run_main(
            "receive", "--incoming", str(archive.parent), "--stem", "example", "--version",
            "1.2.3", "--binary", "example", "--target", LINUX, "--dist", str(self.base / "dist"),
            "--extract-dir", str(self.base / "extracted"), "--source", str(self.source),
            "--root", str(self.root),
        )
        self.assertEqual((code, Path(out.strip()).parent), (0, (self.base / "dist").resolve()))
        listing = self.base / "readobj.txt"
        listing.write_text(ELF + "NeededLibraries [\n]\n", encoding="utf-8")
        self.assertEqual(self.run_main("linkage", "--readobj-output", str(listing), "--target",
                                       LINUX), (0, "\n", ""))
        macho = self.base / "macho"
        macho.write_bytes(macho_header(2, "/usr/lib/libSystem.B.dylib"))
        self.assertEqual(self.run_main("macho-linkage", "--executable", str(macho), "--target",
                                       MACOS_ARM64), (0, "/usr/lib/libSystem.B.dylib\n", ""))
        sbom = self.base / "x.spdx.json"
        sbom.write_text('{"packages": [{"name": "example"}]}', encoding="utf-8")
        self.assertEqual(self.run_main("sbom-lists", "--sbom", str(sbom), "--package", "example"),
                         (0, "x.spdx.json lists example\n", ""))
        self.assertEqual(
            self.run_main("sbom-lists", "--sbom", str(sbom), "--package", "other"),
            (1, "", f"binary_release: {sbom} does not list the package other\n"),
        )
        with mock.patch.object(binary_release, "smoke", return_value="example 1.2.3"):
            self.assertEqual(self.run_main("smoke", "--executable", "x", "--version", "1.2.3"),
                             (0, "example 1.2.3\n", ""))
        code, _, err = self.run_main("verify", "--archive", str(manifest), "--binary", "example",
                                     "--target", LINUX)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("binary_release: "))

    def test_the_script_runs_as_a_command(self) -> None:
        with mock.patch.object(sys, "argv", ["binary_release.py", "--help"]), \
                redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(ROOT / "scripts" / "binary_release.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("cargo-package", out.getvalue())


class HandoffTests(Fixture):
    ARCHIVES = ("source-zip", "source-tar", "linux-archive", "linux-arm64-archive",
                "macos-arm64-archive", "windows-archive")
    run_main = CommandTests.run_main

    def setUp(self) -> None:
        super().setUp()
        self.names = binary_release.handoff_names("example", "1.2.3")
        self.dist, self.incoming = self.base / "dist", self.base / "admitted"

    def hand_off(self) -> None:
        """The admit job's artefact, and this job's own copy of each archive."""
        for directory in (self.dist, self.incoming):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir()
        for role, name in self.names.items():
            (self.incoming / name).write_bytes(f"{role} bytes\n".encode())
            if role in self.ARCHIVES:
                shutil.copyfile(self.incoming / name, self.dist / name)

    def outputs(self) -> list[str]:
        """Digest outputs that match the admit job's artefact as it stands."""
        return [f"{role}={hashlib.sha256((self.incoming / name).read_bytes()).hexdigest()}"
                for role, name in self.names.items()]

    def accept(self, outputs: list[str]) -> tuple[Path, ...]:
        return binary_release.accept(incoming=self.incoming, dist=self.dist, stem="example",
                                     version="1.2.3", digest_outputs=outputs)

    def assert_refused(self, message: str, outputs: list[str]) -> None:
        with self.assertRaisesRegex(ValueError, message):
            self.accept(outputs)

    def assert_no_sbom_kept(self) -> None:
        self.assertEqual(sorted(self.names[role] for role in self.ARCHIVES),
                         sorted(entry.name for entry in self.dist.iterdir()))

    def test_a_matching_hand_off_keeps_the_sboms_beside_this_jobs_archives(self) -> None:
        self.assertEqual(binary_release.asset_name("example", "1.2.3", LINUX),
                         self.names["linux-archive"])
        self.assertEqual(binary_release.asset_name("example", "1.2.3", WINDOWS),
                         self.names["windows-archive"])
        self.assertEqual("example-1.2.3.spdx.json", self.names["source-sbom"])
        # Archives first, so `accept` copies no SBOM until every archive matches.
        self.assertEqual(self.ARCHIVES, tuple(self.names)[:len(self.ARCHIVES)])
        for target in binary_release.TARGETS.values():
            with self.subTest(target=target.triple):
                self.assertEqual(binary_release.asset_name("example", "1.2.3", target.triple),
                                 self.names[f"{target.role}-archive"])
                self.assertEqual(f"example-1.2.3-{target.triple}.spdx.json",
                                 self.names[f"{target.role}-sbom"])
        self.hand_off()
        outputs = binary_release.digests(self.incoming, "example", "1.2.3")
        self.assertEqual(tuple(self.outputs()), outputs)
        self.assertEqual(tuple(self.dist / name for name in self.names.values()),
                         self.accept(list(outputs)))
        for name in self.names.values():
            self.assertEqual((self.incoming / name).read_bytes(), (self.dist / name).read_bytes())
        # An SBOM already in the release is never replaced.
        with self.assertRaises(FileExistsError):
            self.accept(list(outputs))

    def test_a_changed_archive_with_a_forged_matching_digest_is_refused(self) -> None:
        for role in self.ARCHIVES:
            with self.subTest(role=role):
                self.hand_off()
                (self.incoming / self.names[role]).write_bytes(b"changed by the admit job\n")
                self.assert_refused(f"{self.names[role]} from the admit job differs from this "
                                    "job's own", self.outputs())
                self.assert_no_sbom_kept()

    def test_a_file_that_does_not_match_its_digest_output_is_refused(self) -> None:
        for role in ("linux-archive", "linux-sbom"):
            with self.subTest(role=role):
                self.hand_off()
                outputs = self.outputs()
                (self.incoming / self.names[role]).write_bytes(b"damaged in transfer\n")
                self.assert_refused(f"{self.names[role]} does not match the admit job's digest",
                                    outputs)

    def test_a_missing_extra_or_irregular_file_is_refused_before_anything_is_kept(self) -> None:
        sbom = self.names["windows-sbom"]

        def replace_with_directory() -> None:
            (self.incoming / sbom).unlink()
            (self.incoming / sbom).mkdir()

        for case, message, damage in (
            ("missing", "artefact holds", lambda: (self.incoming / sbom).unlink()),
            ("extra", "artefact holds",
             lambda: (self.incoming / "extra.spdx.json").write_bytes(b"{}")),
            ("directory", "is not a regular file", replace_with_directory),
        ):
            with self.subTest(case=case):
                self.hand_off()
                outputs = self.outputs()
                damage()
                self.assert_refused(message, outputs)
                self.assert_no_sbom_kept()

    def test_malformed_missing_repeated_or_unknown_digest_outputs_are_refused(self) -> None:
        self.hand_off()
        outputs = self.outputs()
        role, digest = outputs[0].split("=")
        for malformed in (digest.upper(), digest[:-1], digest + "0", digest + "\n", "",
                          f"sha256:{digest}", "g" * 64):
            with self.subTest(malformed=malformed):
                self.assert_refused(f"{role} digest is not valid",
                                    [f"{role}={malformed}", *outputs[1:]])
        self.assert_refused(f"the {role} digest output is repeated", [*outputs, outputs[0]])
        for wrong in (outputs[:-1], [*outputs, f"other={digest}"]):
            with self.subTest(wrong=wrong):
                self.assert_refused("digest outputs are", wrong)
        self.assert_refused(f"{digest} digest is not valid", [*outputs, digest])
        self.assert_no_sbom_kept()

    def test_a_missing_file_has_no_digest(self) -> None:
        self.hand_off()
        (self.incoming / self.names["source-sbom"]).unlink()
        with self.assertRaises(FileNotFoundError):
            binary_release.digests(self.incoming, "example", "1.2.3")

    def test_the_commands_print_the_outputs_and_the_accepted_files(self) -> None:
        self.hand_off()
        release = ("--stem", "example", "--version", "1.2.3")
        self.assertEqual(self.run_main("digests", "--dist", str(self.incoming), *release),
                         (0, "\n".join(self.outputs()) + "\n", ""))
        accept = ("accept", "--incoming", str(self.incoming), "--dist", str(self.dist), *release)
        self.assertEqual(
            self.run_main(*accept, "--digest", self.outputs()[0]),
            (1, "", f"binary_release: digest outputs are ['source-zip'], expected "
                    f"{sorted(self.names)}\n"),
        )
        digests = [argument for output in self.outputs() for argument in ("--digest", output)]
        self.assertEqual(self.run_main(*accept, *digests),
                         (0, "\n".join(self.names.values()) + "\n", ""))
        self.assertEqual(self.run_main("assets", *release),
                         (0, "\n".join(self.names.values()) + "\n", ""))


class PathArgumentTests(Fixture):
    # Each command's other arguments, then its path arguments.
    COMMANDS = {
        "cargo-package": (("--version", "1.2.3", "--binary", "example"), ("--manifest",)),
        "toolchain": ((), ("--source", "--root")),
        "package": (("--binary", "example", "--target", LINUX, "--stem", "example",
                     "--version", "1.2.3", "--epoch", str(EPOCH)),
                    ("--executables", "--source", "--root", "--output-dir")),
        "verify": (("--binary", "example", "--target", LINUX), ("--archive", "--extract-dir")),
        "receive": (("--stem", "example", "--version", "1.2.3", "--binary", "example",
                     "--target", LINUX),
                    ("--incoming", "--dist", "--extract-dir", "--source", "--root")),
        "smoke": (("--version", "1.2.3"), ("--executable",)),
        "linkage": (("--target", LINUX), ("--readobj-output",)),
        "macho-linkage": (("--target", MACOS_ARM64), ("--executable",)),
        "sbom-lists": (("--package", "example"), ("--sbom",)),
        "digests": (("--stem", "example", "--version", "1.2.3"), ("--dist",)),
        "accept": (("--stem", "example", "--version", "1.2.3", "--digest", "source-zip="),
                   ("--incoming", "--dist")),
    }
    run_main = CommandTests.run_main

    def setUp(self) -> None:
        super().setUp()
        # The working and temporary directory is `trusted`; `outside` is its sibling.
        self.trusted, self.outside = self.base / "trusted", self.base / "outside"
        self.trusted.mkdir()
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.trusted)
        environment = {name: value for name, value in os.environ.items()
                       if name not in binary_release._RUNNER_DIRECTORIES}
        for patcher in (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(tempfile, "gettempdir", return_value=str(self.trusted)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_every_path_argument_that_escapes_is_refused_before_use(self) -> None:
        escape = str(self.trusted / ".." / "outside" / "x")
        for command, (others, paths) in self.COMMANDS.items():
            for refused in paths:
                argv = [command, *others]
                for option in paths:
                    argv += [option, escape if option == refused else "inside"]
                with self.subTest(command=command, option=refused):
                    self.assertEqual(self.run_main(*argv), (1, "", (
                        f"binary_release: {escape} is outside the working, workspace and "
                        "temporary directories\n")))
        self.assertFalse(self.outside.exists())

    def test_paths_inside_the_working_or_runner_directories_are_used_resolved(self) -> None:
        (self.trusted / "Cargo.toml").write_text(PACKAGE, encoding="utf-8")
        self.assertEqual(self.run_main("cargo-package", "--manifest", "Cargo.toml", "--version",
                                       "1.2.3", "--binary", "example"), (0, "example\n", ""))
        self.outside.mkdir()
        (self.outside / "x.spdx.json").write_text('{"packages": [{"name": "example"}]}',
                                                  encoding="utf-8")
        sbom = ("sbom-lists", "--sbom", str(self.trusted / ".." / "outside" / "x.spdx.json"),
                "--package", "example")
        self.assertEqual(self.run_main(*sbom)[0], 1)
        for name in ("RUNNER_TEMP", "CARGO_TARGET_DIR"):
            with self.subTest(base=name), mock.patch.dict(os.environ, {name: str(self.outside)}):
                self.assertEqual(self.run_main(*sbom), (0, "x.spdx.json lists example\n", ""))
        with mock.patch.object(binary_release, "verify", return_value=("example",)) as verify:
            self.run_main("verify", "--archive", "a.tar.gz", "--binary", "example", "--target",
                          LINUX, "--extract-dir", "out")
        self.assertEqual(verify.call_args.kwargs["archive"], self.trusted.resolve() / "a.tar.gz")
        self.assertEqual(verify.call_args.kwargs["extract_dir"], self.trusted.resolve() / "out")

    def test_a_linked_path_argument_is_refused_before_it_is_resolved(self) -> None:
        with mock.patch.object(Path, "is_symlink", return_value=True):
            self.assertEqual(
                self.run_main("cargo-package", "--manifest", "Cargo.toml", "--version", "1.2.3",
                              "--binary", "example"),
                (1, "", "binary_release: Cargo.toml must not be a symbolic link\n"),
            )


def member_bytes(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.extractfile(name)
    assert member is not None
    return member.read()


if __name__ == "__main__":
    unittest.main()
