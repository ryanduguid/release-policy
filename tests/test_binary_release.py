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
import runpy
import shutil
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
WINDOWS = "x86_64-pc-windows-msvc"
EPOCH = 1_790_000_000
PACKAGE = '[package]\nname = "example"\nversion = "1.2.3"\npublish = false\n'
# The header lines llvm-readobj prints for each target's executable.
ELF = "File: example\nFormat: elf64-x86-64\nArch: x86_64\nAddressSize: 64bit\n"
COFF = "File: example.exe\nFormat: COFF-x86-64\nArch: x86_64\nAddressSize: 64bit\n"


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

    def build(self, triple: str, output: str = "dist") -> Path:
        return binary_release.package(
            executable=self.executable, binary="example", triple=triple, stem="example",
            version="1.2.3", source=self.source, root=self.root, epoch=EPOCH,
            output_dir=self.base / output,
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
            executable=self.executable, binary="example", triple=WINDOWS, stem="example",
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
            ("example", "1.2.3", "aarch64-apple-darwin", "unsupported target"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                binary_release.asset_name(stem, version, triple)
        with self.assertRaisesRegex(ValueError, "binary is not valid"):
            binary_release.member_names("../x", LINUX, ())


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

    def test_too_many_or_oversized_members_are_refused_before_reading(self) -> None:
        exe = (self.info("example", 0o755), b"x")
        docs = [(self.info(name), b"d") for name in ("LICENSE", "NOTICE", "README.md", "extra")]
        with self.assertRaisesRegex(ValueError, "extra exceeds the archive's member count"):
            binary_release.verify(archive=self.tar([exe, *docs]), binary="example", triple=LINUX,
                                  extract_dir=None)
        with mock.patch.object(binary_release, "MAX_MEMBER_BYTES", 0):
            with self.assertRaisesRegex(ValueError, "example exceeds"):
                binary_release.verify(archive=self.tar([exe]), binary="example", triple=LINUX,
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
    def artefact(self, triple: str = LINUX) -> Path:
        incoming = self.base / f"incoming-{triple}"
        archive = self.build(triple, incoming.name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (incoming / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n",
                                                         encoding="utf-8")
        return incoming

    def receive(self, incoming: Path, triple: str = LINUX) -> Path:
        return binary_release.receive(
            incoming=incoming, stem="example", version="1.2.3", binary="example",
            triple=triple, dist=self.base / "dist", extract_dir=self.base / "extracted",
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
            )

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
            shutil.rmtree(incoming)
        incoming = self.artefact()
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
        self.assertEqual(binary_release.linkage(static, LINUX, None), ())
        for output in (static.replace("NeededLibraries [\n", "NeededLibraries [\n  libc.so\n"),
                       static.replace("PT_LOAD (0x1)", "PT_INTERP (0x3)")):
            with self.assertRaisesRegex(ValueError, "not a static executable"):
                binary_release.linkage(output, LINUX, None)
        with self.assertRaisesRegex(ValueError, "no NeededLibraries list"):
            binary_release.linkage(ELF, LINUX, None)
        for wrong in (static.replace("Arch: x86_64", "Arch: aarch64"), COFF + "NeededLibraries [\n]\n"):
            with self.assertRaisesRegex(ValueError, "not an x86-64 elf64-x86-64 executable"):
                binary_release.linkage(wrong, LINUX, None)

    def test_windows_imports_only_system_dlls_and_no_c_runtime(self) -> None:
        system = self.base / "System32"
        system.mkdir()
        for name in ("KERNEL32.dll", "ntdll.dll", "VCRUNTIME140.dll"):
            (system / name).write_bytes(b"")
        # The imports of release-policy's own static-CRT build, 28 October 2026.
        listing = (COFF + "NeededLibraries [\r\n  KERNEL32.dll\r\n"
                   "  api-ms-win-core-synch-l1-2-0.dll\r\n  ntdll.dll\r\n]\r\n")
        self.assertEqual(binary_release.linkage(listing, WINDOWS, system),
                         ("KERNEL32.dll", "api-ms-win-core-synch-l1-2-0.dll", "ntdll.dll"))
        with self.assertRaisesRegex(ValueError, "C runtime DLLs: \\['VCRUNTIME140.dll'"):
            binary_release.linkage(listing.replace("ntdll", "VCRUNTIME140"), WINDOWS, system)
        with self.assertRaisesRegex(ValueError, "C runtime DLLs"):
            binary_release.linkage(listing.replace("ntdll", "api-ms-win-crt-heap-l1-1-0"),
                                   WINDOWS, system)
        with self.assertRaisesRegex(ValueError, "does not ship: \\['example-helper.dll'"):
            binary_release.linkage(listing.replace("ntdll", "example-helper"), WINDOWS, system)
        with self.assertRaisesRegex(ValueError, "needs --system-directory"):
            binary_release.linkage(listing, WINDOWS, None)

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
                   "--version", "1.2.3", "--executable", str(self.executable), "--source",
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
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        code, out, _ = self.run_main(
            "receive", "--incoming", str(archive.parent), "--stem", "example", "--version",
            "1.2.3", "--binary", "example", "--target", LINUX, "--dist", str(self.base / "dist"),
            "--extract-dir", str(self.base / "extracted"),
        )
        self.assertEqual((code, Path(out.strip()).parent), (0, self.base / "dist"))
        listing = self.base / "readobj.txt"
        listing.write_text(ELF + "NeededLibraries [\n]\n", encoding="utf-8")
        self.assertEqual(self.run_main("linkage", "--readobj-output", str(listing), "--target",
                                       LINUX), (0, "\n", ""))
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


if __name__ == "__main__":
    unittest.main()
