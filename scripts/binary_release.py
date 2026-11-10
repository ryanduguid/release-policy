# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""Package, check and validate the per-target archives of a Rust binary release.

The binary release family (`release-binary.yml`) builds each target in its own
read-only job, checks the archives with third-party tools in a read-only admit
job and publishes from one privileged job that runs neither consumer code nor
those tools. The build jobs call `package`, `smoke`, and `linkage` or, for
macOS, `macho-linkage`. The admit job calls `receive`, the same linkage
command, `sbom-lists` and `digests`. The publisher calls
`cargo-package` before anything is built, `receive` on each archive again,
`accept` on what the admit job hands over and `assets` for the release
inventory, so a missing, extra, unsafe or changed file stops the release.
`--binary` names one or more Cargo binary targets separated by spaces, and
`--data-files` the files they need at run time, one path per line relative to
the component; each archive ships all of them.

Every output path comes from the workflow or build_binary.sh, under the
runner's temporary directory or the checkout's dist directory; member names
are checked before anything is extracted. A path argument that resolves
outside the working, workspace and temporary directories (or Cargo's
CARGO_TARGET_DIR) is refused, so outside a runner set GITHUB_WORKSPACE to the
checkout. A Cargo config target-dir outside those directories is unsupported.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess  # nosec B404: runs only the built executable's --version
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Sequence, cast

_STEM = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_BINARY = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


_DATA_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*\Z")


@dataclass(frozen=True)
class Target:
    triple: str
    suffix: str
    executable_suffix: str
    # llvm-readobj's Format and Arch lines for an executable of this target.
    object_format: str
    arch: str
    # Names the job outputs that carry this target's archive and SBOM digests.
    role: str


TARGETS = {
    target.triple: target
    for target in (
        Target("x86_64-unknown-linux-musl", ".tar.gz", "", "elf64-x86-64", "x86_64", "linux"),
        Target("aarch64-unknown-linux-musl", ".tar.gz", "", "elf64-littleaarch64", "aarch64",
               "linux-arm64"),
        Target("aarch64-apple-darwin", ".tar.gz", "", "Mach-O arm64", "aarch64", "macos-arm64"),
        Target("x86_64-pc-windows-msvc", ".zip", ".exe", "COFF-x86-64", "x86_64", "windows"),
    )
}
# An archive holds the executables, the declared data files and up to 3
# documents, at most this many in all. The size bounds are far above any
# expected release. They apply to the archive file, to each member and to
# everything decompressed, so neither a member nor the archive's own metadata
# (PAX headers, a ZIP central directory) can make the publisher read without
# limit.
MAX_MEMBERS = 12
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_MEMBER_BYTES
MAX_DECOMPRESSED_BYTES = MAX_MEMBER_BYTES + 64 * 1024
_MAX_DIGEST_BYTES = 1024
_ZIP_END = b"PK\x05\x06"
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP_END_SEARCH = 22 + 0xFFFF
_MAX_ZIP_DIRECTORY_BYTES = 64 * 1024
_TOOLCHAIN_CHANNEL = re.compile(r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_DIGEST_LINE = re.compile(r"(?P<digest>[0-9a-f]{64}) [ *](?P<name>[^\r\n]+)\r?\n\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# The files the admit job hands to the publisher, by the name of the job output
# that carries each one's SHA-256, and the suffix after `<stem>-<version>`. The
# publisher rebuilds or re-admits each archive itself; it takes the SBOMs, the
# roles ending in -sbom, because making them needs Syft, which it never runs.
# Archives come first, so `accept` copies no SBOM until every archive matches.
HANDOFF = {
    "source-zip": ".zip",
    "source-tar": ".tar.gz",
    **{f"{t.role}-archive": f"-{t.triple}{t.suffix}" for t in TARGETS.values()},
    "source-sbom": ".spdx.json",
    **{f"{t.role}-sbom": f"-{t.triple}.spdx.json" for t in TARGETS.values()},
}
# Documents copied beside the executable. LICENSE and README.md are required;
# NOTICE ships only when the component or repository has one.
DOCUMENTS = ("LICENSE", "NOTICE", "README.md")
REQUIRED_DOCUMENTS = frozenset({"LICENSE", "README.md"})
# A static musl executable names no shared library and no program interpreter.
# A Windows executable built with the static C runtime must not import the
# runtime DLLs it replaced, and may import only DLLs Windows itself ships.
_WINDOWS_RUNTIME = re.compile(r"(?:vcruntime|msvcp|msvcr|ucrtbase|api-ms-win-crt-).*", re.IGNORECASE)
# A bare DLL file name: a path in an import would escape the System32 lookup.
_DLL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.dll", re.IGNORECASE)
# The DLLs an executable may import: Windows components every supported
# Windows ships. Pinned here, not read from a System32 directory, so the
# Linux publisher applies the same policy as the Windows build job. Add a
# name only for a consumer that needs it.
WINDOWS_SYSTEM_DLLS = frozenset({
    "advapi32.dll", "bcrypt.dll", "bcryptprimitives.dll", "crypt32.dll", "dbghelp.dll",
    "iphlpapi.dll", "kernel32.dll", "mswsock.dll", "ncrypt.dll", "ntdll.dll", "ole32.dll",
    "oleaut32.dll", "psapi.dll", "rpcrt4.dll", "secur32.dll", "shell32.dll", "shlwapi.dll",
    "user32.dll", "userenv.dll", "version.dll", "winhttp.dll", "ws2_32.dll",
})
# API set names, which Rust's standard library imports, are resolved by the
# Windows loader and have no file of that name in System32.
_API_SET = re.compile(r"(?:api|ext)-ms-win-[a-z0-9-]+\.dll", re.IGNORECASE)
# macOS has no static executables: each one loads libSystem through dyld. One
# may load only libraries macOS itself ships, by absolute path on the sealed
# system volume: a library directly in /usr/lib or a system framework. A
# library found through @rpath, @executable_path or @loader_path, or installed
# elsewhere (Homebrew, /usr/local), is one the release does not ship.
_MACOS_SYSTEM_LIBRARY = re.compile(
    r"/usr/lib/[A-Za-z0-9_+-][A-Za-z0-9_.+-]*\.dylib"
    r"|/System/Library/Frameworks/(?P<name>[A-Za-z0-9_]+)\.framework/Versions/[A-Z0-9]+/(?P=name)"
)
# A macOS executable's linkage is read from its own load commands, not from
# llvm-readobj, which reports each slice of a universal file separately and
# does not show LC_DYLD_ENVIRONMENT. Values from <mach-o/loader.h>: the thin
# 64-bit little-endian magic, CPU_TYPE_ARM64 and MH_EXECUTE, then the load
# commands that make dyld load a library (LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB,
# LC_REEXPORT_DYLIB, LC_LAZY_LOAD_DYLIB, LC_LOAD_UPWARD_DYLIB) and those that
# change where or how dyld looks (LC_LOAD_DYLINKER, LC_RPATH,
# LC_DYLD_ENVIRONMENT).
_MACHO_MAGIC_64 = b"\xcf\xfa\xed\xfe"
_MACHO_ARM64 = 0x0100000C
_MACHO_EXECUTE = 0x2
_MACHO_LOAD_LIBRARY = frozenset({0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023})
_MACHO_LOAD_DYLINKER = 0xE
_MACHO_REFUSED = {0x8000001C: "adds a run-path search path",
                  0x27: "sets dyld environment variables"}
_MACHO_MALFORMED = "malformed Mach-O load commands"
_NEEDED = re.compile(r"NeededLibraries \[\n(?P<names>(?:[^\]\n]*\n)*?)\]", re.MULTILINE)
# A runner's checkout and its temporary directory: the workflows pass paths
# under both, and build_binary.sh passes the checkout above its own directory.
# A consumer may also build into a CARGO_TARGET_DIR outside both.
_RUNNER_DIRECTORIES = ("GITHUB_WORKSPACE", "RUNNER_TEMP", "CARGO_TARGET_DIR")


def _target(triple: str) -> Target:
    try:
        return TARGETS[triple]
    except KeyError:
        raise ValueError(f"unsupported target: {triple}") from None


def _checked(pattern: re.Pattern[str], value: str, label: str) -> str:
    if pattern.match(value) is None:
        raise ValueError(f"{label} is not valid: {value!r}")
    return value


def asset_name(stem: str, version: str, triple: str) -> str:
    _checked(_STEM, stem, "artifact-stem")
    _checked(_VERSION, version, "version")
    return f"{stem}-{version}-{triple}{_target(triple).suffix}"


def binaries(binary: str) -> tuple[str, ...]:
    """The Cargo binary targets a release ships, named in order and separated by spaces."""
    names = tuple(_checked(_BINARY, name, "binary") for name in binary.split())
    if not names or len(set(names)) != len(names):
        raise ValueError(f"binary must name one or more distinct targets: {binary!r}")
    return names


def data_names(data_files: str) -> dict[str, str]:
    """Each declared data file by the name it ships under, the last part of its path.

    `data_files` holds one path per line, relative to the component directory.
    """
    names: dict[str, str] = {}
    for entry in (line.strip() for line in data_files.splitlines()):
        if not entry:
            continue
        name = _checked(_DATA_FILE, entry, "data file").rsplit("/", 1)[-1]
        if name in names or name in DOCUMENTS:
            raise ValueError(f"data file {entry} would ship as {name}, a name already taken")
        names[name] = entry
    return names


def member_names(
    binary: str, triple: str, documents: Sequence[str], data: Sequence[str] = (),
) -> tuple[str, ...]:
    """The executables in the order named, then the data files and documents sorted."""
    suffix = _target(triple).executable_suffix
    names = (*(name + suffix for name in binaries(binary)), *sorted([*data, *documents]))
    if len(set(names)) != len(names) or len(names) > MAX_MEMBERS:
        raise ValueError(f"the archive cannot hold {list(names)}: at most {MAX_MEMBERS} "
                         "distinct members")
    return names


def cargo_package(manifest: Path, release_version: str, binary: str) -> str:
    """The package name, once the manifest agrees with the release.

    The literal `[package] version` must equal the tracked VERSION, and each
    binary must be the package's own name or a declared `[[bin]]` target.
    `publish = false` keeps the package off crates.io (Decision 7).
    The manifest is read as data; Cargo never runs in the publishing job.
    """
    if manifest.is_symlink():
        raise ValueError(f"{manifest} must not be a symbolic link")
    try:
        document = tomllib.loads(manifest.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{manifest} is not a readable Cargo manifest: {error}") from None
    package = document.get("package")
    if not isinstance(package, dict):
        raise ValueError(f"{manifest} has no [package] table")
    name, version = package.get("name"), package.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise ValueError(
            f"{manifest} must state a literal package name and version; "
            "workspace inheritance is not supported"
        )
    _checked(_VERSION, version, f"{manifest} package version")
    if version != release_version:
        raise ValueError(f"Cargo.toml version {version} != release version {release_version}")
    if package.get("publish") is not False:
        raise ValueError(f"{manifest} must set publish = false: binaries ship on GitHub Releases only")
    bins = document.get("bin", [])
    declared = {item.get("name") for item in bins if isinstance(item, dict)} if isinstance(
        bins, list) else set()
    for each in binaries(binary):
        if each not in declared | {name}:
            raise ValueError(f"{each} is neither the package {name} nor a declared [[bin]]")
    return name


def find_documents(source: Path, root: Path) -> dict[str, Path]:
    """Each document from the component directory, else the repository root."""
    found: dict[str, Path] = {}
    for name in DOCUMENTS:
        for directory in (source, root):
            candidate = directory / name
            if candidate.is_file() and not candidate.is_symlink():
                found[name] = candidate
                break
    missing = sorted(REQUIRED_DOCUMENTS - set(found))
    if missing:
        raise ValueError(f"missing release documents: {', '.join(missing)}")
    return found


def find_data_files(source: Path, data_files: str) -> dict[str, Path]:
    """Each declared data file, a regular file inside the component directory."""
    found: dict[str, Path] = {}
    for name, entry in data_names(data_files).items():
        path = source / entry
        # Resolving follows every link on the way, so a path through a linked
        # directory or to a linked file no longer resolves to itself.
        if path.resolve() != source.resolve() / entry or not path.is_file():
            raise ValueError(f"data file {entry} is not a regular file in {source}")
        found[name] = path
    return found


def _write_tar_gz(output: Path, members: list[tuple[str, bytes, int]], epoch: int) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, epoch
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    with output.open("xb") as handle:  # NOSONAR
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as compressed:
            compressed.write(buffer.getvalue())


def _write_zip(output: Path, members: list[tuple[str, bytes, int]], epoch: int) -> None:
    # ZIP stores a timestamp with 2-second resolution from 1980.
    stamp = time.gmtime(max(epoch, 315532800))[:6]
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:  # NOSONAR
        for name, data, mode in members:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, data)


def package(
    *, executables: Path, binary: str, triple: str, stem: str, version: str,
    source: Path, root: Path, epoch: int, output_dir: Path, data_files: str = "",
) -> Path:
    """Archive each named executable from the `executables` directory with its files."""
    target = _target(triple)
    output = output_dir / asset_name(stem, version, triple)
    documents = find_documents(source, root)
    data = find_data_files(source, data_files)
    files = {**documents, **data}
    names = member_names(binary, triple, tuple(documents), tuple(data))
    count = len(binaries(binary))
    members = [(name, (executables / name).read_bytes(), 0o755) for name in names[:count]]
    members += [(name, files[name].read_bytes(), 0o644) for name in names[count:]]
    output_dir.mkdir(parents=True, exist_ok=True)
    if target.suffix == ".zip":
        _write_zip(output, members, epoch)
    else:
        _write_tar_gz(output, members, epoch)
    return output


def _admit(members: dict[str, tuple[bytes, int]], name: str, size: int) -> None:
    if name in members:
        raise ValueError(f"duplicate member {name}")
    total = size + sum(len(data) for data, _ in members.values())
    if len(members) >= MAX_MEMBERS or size > MAX_MEMBER_BYTES or total > MAX_DECOMPRESSED_BYTES:
        raise ValueError(f"{name} exceeds the archive's member count or size limit")


class _Bounded(io.RawIOBase):
    """A reader that fails once more than `limit` bytes have been read."""

    def __init__(self, stream: io.BufferedIOBase, limit: int) -> None:
        self._stream, self._left = stream, limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        data = self._stream.read(min(len(buffer), self._left + 1))
        self._left -= len(data)
        if self._left < 0:
            raise ValueError("the archive decompresses beyond the size limit")
        buffer[: len(data)] = data
        return len(data)


def _zip_directory_is_small(archive: Path) -> None:
    """Refuse a ZIP whose central directory is large before zipfile parses it."""
    size = archive.stat().st_size
    with archive.open("rb") as handle:
        # 20 more bytes, so a ZIP64 locator before a record that ends a
        # maximum-length comment is still read.
        handle.seek(max(0, size - _ZIP_END_SEARCH - 20))
        tail = handle.read()
    end = tail.rfind(_ZIP_END)
    if end < 0 or len(tail) - end < 22:
        raise ValueError(f"{archive.name} has no ZIP end of central directory record")
    # zipfile follows a ZIP64 locator just before this record, whose 64-bit
    # counts and sizes would override the ones checked here. With end < 20
    # the record is within 20 bytes of the file's start, leaving no room.
    if end >= 20 and tail[end - 20 : end - 16] == _ZIP64_LOCATOR:
        raise ValueError(f"{archive.name} is a ZIP64 archive; release archives are not")
    _, _, on_disk, total, directory, _, _ = struct.unpack("<HHHHIIH", tail[end + 4 : end + 22])
    if on_disk != total or total > MAX_MEMBERS or directory > _MAX_ZIP_DIRECTORY_BYTES:
        raise ValueError(f"{archive.name} lists {total} members in a {directory}-byte directory")


def _archive_members(archive: Path, target: Target) -> dict[str, tuple[bytes, int]]:
    members: dict[str, tuple[bytes, int]] = {}
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"{archive.name} exceeds the archive size limit")
    if target.suffix == ".zip":
        _zip_directory_is_small(archive)
        with zipfile.ZipFile(archive) as opened:
            for info in opened.infolist():
                mode = (info.external_attr >> 16) & 0o177777
                kind = mode & 0o170000
                if info.is_dir() or (kind and kind != 0o100000):
                    raise ValueError(f"{info.filename} is not a regular file")
                _admit(members, info.filename, info.file_size)
                # Reads stop at the declared size, and its CRC is checked.
                members[info.filename] = (opened.read(info), mode & 0o7777)
        return members
    # A stream read through a bound: tarfile reads PAX and GNU extension
    # headers before yielding a member, so they count against the limit too.
    with gzip.open(archive, "rb") as compressed, tarfile.open(
        None, "r|", io.BufferedReader(_Bounded(compressed, MAX_DECOMPRESSED_BYTES))
    ) as packed:
        for member in packed:
            if member.pax_headers or packed.pax_headers:
                raise ValueError(f"{member.name} uses PAX headers; release archives are USTAR")
            if not member.isreg():
                raise ValueError(f"{member.name} is not a regular file")
            _admit(members, member.name, member.size)
            # A regular member always has a file object.
            extracted = cast(IO[bytes], packed.extractfile(member))
            members[member.name] = (extracted.read(), member.mode & 0o7777)
    return members


def verify(
    *, archive: Path, binary: str, triple: str, extract_dir: Path | None, data_files: str = "",
) -> tuple[str, ...]:
    """Require exactly the release members, and optionally extract them."""
    target = _target(triple)
    members = _archive_members(archive, target)
    executables = [name + target.executable_suffix for name in binaries(binary)]
    data = tuple(data_names(data_files))
    documents = sorted(set(members) - {*executables, *data})
    expected = member_names(binary, triple, documents, data)
    for name in members:
        path = PurePosixPath(name)
        if len(path.parts) != 1 or name in {".", ".."} or "\\" in name:
            raise ValueError(f"unsafe member name {name!r}")
    for kind, names in (("executable", executables), ("data file", data)):
        for name in names:
            if name not in members:
                raise ValueError(f"the {kind} {name} is missing")
    if set(documents) - set(DOCUMENTS) or not REQUIRED_DOCUMENTS <= set(documents):
        raise ValueError(f"members are {sorted(members)}, expected {list(expected)}")
    for name in executables:
        if target.executable_suffix == "" and not members[name][1] & 0o100:
            raise ValueError(f"{name} is not executable")
    if extract_dir is not None:
        extract_dir.mkdir(parents=True, exist_ok=False)
        for name in expected:
            (extract_dir / name).write_bytes(members[name][0])  # NOSONAR
            os.chmod(extract_dir / name, members[name][1] or 0o644)  # NOSONAR
    return expected


def smoke(executable: Path, version: str) -> str:
    result = subprocess.run(  # NOSONAR  # nosec B603: an argument list, no shell
        [str(executable), "--version"], capture_output=True, text=True, check=False,
        timeout=60,
    )
    first = result.stdout.splitlines()[0] if result.stdout else ""
    if result.returncode != 0 or not first.endswith(f" {version}"):
        raise ValueError(
            f"{executable.name} --version exited {result.returncode} with {first!r}; "
            f"expected a first line ending in ' {version}'"
        )
    return first


def needed_libraries(readobj_output: str) -> tuple[str, ...]:
    match = _NEEDED.search(readobj_output.replace("\r\n", "\n"))
    if match is None:
        raise ValueError("llvm-readobj printed no NeededLibraries list")
    return tuple(line.strip() for line in match.group("names").splitlines() if line.strip())


def _macho_load_commands(executable: bytes) -> tuple[int, int]:
    """The load command count of a thin arm64 Mach-O executable, and where its commands end."""
    if executable[:4] != _MACHO_MAGIC_64:
        raise ValueError("not a thin 64-bit little-endian Mach-O file")
    if len(executable) < 32:
        raise ValueError(_MACHO_MALFORMED)
    _, cputype, _, filetype, count, size = struct.unpack_from("<6I", executable)
    if cputype != _MACHO_ARM64:
        raise ValueError("not an arm64 Mach-O executable")
    if filetype != _MACHO_EXECUTE:
        raise ValueError("not a Mach-O executable file type")
    if 32 + size > len(executable):
        raise ValueError(_MACHO_MALFORMED)
    return count, 32 + size


def _macho_path(executable: bytes, offset: int, length: int) -> str:
    """The path a library or dylinker command names, at the offset its third field holds."""
    name_at = struct.unpack_from("<I", executable, offset + 8)[0] if length >= 12 else 0
    name, terminated, _ = executable[offset + name_at:offset + length].partition(b"\0")
    if not 12 <= name_at < length or not terminated:
        raise ValueError(_MACHO_MALFORMED)
    try:
        return name.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(_MACHO_MALFORMED) from None


def macho_linkage(executable: bytes, triple: str) -> tuple[str, ...]:
    """The libraries a thin arm64 Mach-O executable loads, once each is one macOS ships.

    A universal file is refused whole, since a loader picks a slice this
    check would not see, and so is any command that points dyld elsewhere.
    """
    if not _target(triple).object_format.startswith("Mach-O"):
        raise ValueError(f"only a macOS executable's linkage is read from it, not {triple}'s")
    count, end = _macho_load_commands(executable)
    offset, needed = 32, []
    for _ in range(count):
        if offset + 8 > end:
            raise ValueError(_MACHO_MALFORMED)
        command, length = struct.unpack_from("<2I", executable, offset)
        if length < 8 or offset + length > end:
            raise ValueError(_MACHO_MALFORMED)
        if command in _MACHO_REFUSED:
            raise ValueError(f"the executable {_MACHO_REFUSED[command]}")
        if command in _MACHO_LOAD_LIBRARY:
            needed.append(_macho_path(executable, offset, length))
        elif command == _MACHO_LOAD_DYLINKER:
            text = _macho_path(executable, offset, length)
            if text != "/usr/lib/dyld":
                raise ValueError("the executable names a dynamic linker other than "
                                 f"/usr/lib/dyld: {text}")
        offset += length
    if offset != end:
        raise ValueError(_MACHO_MALFORMED)
    foreign = [name for name in needed if not _MACOS_SYSTEM_LIBRARY.fullmatch(name)]
    if foreign:
        raise ValueError(f"loads libraries macOS does not ship: {foreign}")
    return tuple(needed)


def linkage(readobj_output: str, triple: str) -> tuple[str, ...]:
    target = _target(triple)
    if target.object_format.startswith("Mach-O"):
        raise ValueError("a macOS executable's linkage is read from its load commands")
    # llvm-readobj prints one block for each object in a file; one clean
    # block must not stand for another.
    if readobj_output.count("Format: ") != 1 or readobj_output.count("NeededLibraries [") > 1:
        raise ValueError("llvm-readobj's output describes more than one object")
    lines = {line.strip() for line in readobj_output.splitlines()}
    if not {f"Format: {target.object_format}", f"Arch: {target.arch}"} <= lines:
        raise ValueError(f"not an {target.arch} {target.object_format} executable")
    needed = needed_libraries(readobj_output)
    if target.object_format.startswith("elf"):
        if needed or "PT_INTERP" in readobj_output:
            raise ValueError(f"not a static executable: needs {list(needed)} or an interpreter")
        return needed
    runtime = [name for name in needed if _WINDOWS_RUNTIME.fullmatch(name)]
    if runtime:
        raise ValueError(f"imports C runtime DLLs: {runtime}")
    unsafe = [name for name in needed if not _DLL_NAME.fullmatch(name)]
    if unsafe:
        raise ValueError(f"imports DLLs by path or with an unexpected name: {unsafe}")
    foreign = [
        name for name in needed
        if not _API_SET.fullmatch(name) and name.lower() not in WINDOWS_SYSTEM_DLLS
    ]
    if foreign:
        raise ValueError(f"imports DLLs outside the pinned Windows system set: {foreign}")
    return needed


def toolchain(source: Path, root: Path) -> str:
    """The exact toolchain rustup will select for the component (Decision 1).

    rustup takes the nearest toolchain file from the build directory upwards,
    so the search stops at the first directory holding either file name.
    """
    source, root = source.resolve(), root.resolve()
    if source != root and root not in source.parents:
        raise ValueError(f"{source} is not inside {root}")
    chain = [source, *source.parents]
    for directory in chain[: chain.index(root) + 1]:
        legacy, current = directory / "rust-toolchain", directory / "rust-toolchain.toml"
        if legacy.exists() or legacy.is_symlink():
            raise ValueError(f"{legacy}: use rust-toolchain.toml with an exact channel")
        if current.is_symlink() or (current.exists() and not current.is_file()):
            raise ValueError(f"{current} must be a regular file")
        if current.is_file():
            try:
                document = tomllib.loads(current.read_bytes().decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                raise ValueError(f"{current} is not readable TOML: {error}") from None
            table = document.get("toolchain")
            channel = table.get("channel") if isinstance(table, dict) else None
            if not isinstance(channel, str) or _TOOLCHAIN_CHANNEL.match(channel) is None:
                raise ValueError(f"{current} must pin an exact MAJOR.MINOR.PATCH channel")
            return channel
    raise ValueError(f"no rust-toolchain.toml between {source} and {root}")


def receive(
    *, incoming: Path, stem: str, version: str, binary: str, triple: str,
    dist: Path, extract_dir: Path, source: Path, root: Path, data_files: str = "",
) -> Path:
    """Admit one build job's artefact into the release, or refuse it.

    The artefact must hold exactly the archive and its digest line, the
    digest must match the bytes, and the archive must pass `verify`. The
    digest only proves the transfer: the build job wrote both files. The
    archive is copied into the release first, and the copy is what is
    hashed, checked and kept. Its documents must be byte for byte the ones
    in the tagged tree at `source` (else `root`), and its data files the
    declared ones at `source`.
    """
    name = asset_name(stem, version, triple)
    expected = {name, f"{name}.sha256"}
    entries = {entry.name: entry for entry in incoming.iterdir()}
    if set(entries) != expected:
        raise ValueError(f"artefact holds {sorted(entries)}, expected {sorted(expected)}")
    for entry in entries.values():
        if entry.is_symlink() or not entry.is_file():
            raise ValueError(f"{entry.name} is not a regular file")
    digest_file, archive = entries[f"{name}.sha256"], entries[name]
    if digest_file.stat().st_size > _MAX_DIGEST_BYTES:
        raise ValueError(f"{name}.sha256 is not one sha256sum line for {name}")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"{name} exceeds the archive size limit")
    line = digest_file.read_bytes().decode("utf-8", errors="replace")
    match = _DIGEST_LINE.match(line)
    if match is None or match.group("name") != name:
        raise ValueError(f"{name}.sha256 is not one sha256sum line for {name}")
    tagged = {**find_documents(source, root), **find_data_files(source, data_files)}
    dist.mkdir(parents=True, exist_ok=True)
    admitted = dist / name
    admitted.open("xb").close()  # NOSONAR: never replace an earlier admission
    try:
        with archive.open("rb") as incoming_archive, admitted.open("wb") as handle:
            shutil.copyfileobj(incoming_archive, handle)
        with admitted.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != match.group("digest"):
            raise ValueError(f"{name} does not match its recorded digest")
        names = verify(archive=admitted, binary=binary, triple=triple, extract_dir=extract_dir,
                       data_files=data_files)
        shipped = names[len(binaries(binary)):]
        if set(shipped) != set(tagged):
            raise ValueError(f"{name} ships {sorted(shipped)}, the tag has {sorted(tagged)}")
        for document in shipped:
            if (extract_dir / document).read_bytes() != tagged[document].read_bytes():
                raise ValueError(f"{name}'s {document} differs from the tagged {document}")
    except BaseException:
        admitted.unlink()
        raise
    return admitted


def sbom_lists(sbom: Path, package_name: str) -> bool:
    document = json.loads(sbom.read_bytes().decode("utf-8"))
    packages = document.get("packages") if isinstance(document, dict) else None
    return isinstance(packages, list) and any(
        isinstance(item, dict) and item.get("name") == package_name for item in packages
    )


def handoff_names(stem: str, version: str) -> dict[str, str]:
    """Each file the admit job hands over, by the job output holding its digest."""
    base = f"{_checked(_STEM, stem, 'artifact-stem')}-{_checked(_VERSION, version, 'version')}"
    return {role: base + suffix for role, suffix in HANDOFF.items()}


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def digests(dist: Path, stem: str, version: str) -> tuple[str, ...]:
    """The admit job's outputs: one `<role>=<SHA-256>` line for each file it hands over."""
    return tuple(
        f"{role}={_checked(_SHA256, _sha256(dist / name), f'{name} digest')}"
        for role, name in handoff_names(stem, version).items()
    )


def accept(
    *, incoming: Path, dist: Path, stem: str, version: str, digest_outputs: Sequence[str],
) -> tuple[Path, ...]:
    """Take the admit job's SBOMs into the release once its whole hand-off checks out.

    The admit job ran third-party tools, so its artefact and its outputs are
    untrusted: a compromised job could change a file and its digest together.
    The digests only prove the transfer. Each archive must also be byte for
    byte the one already in `dist`, which this job built or admitted itself.
    Each SBOM is copied into `dist` first, and the copy is what is hashed and
    kept. Each `digest_outputs` item is `<role>=<SHA-256>`, once per role.
    """
    names = handoff_names(stem, version)
    expected: dict[str, str] = {}
    for item in digest_outputs:
        role, _, digest = item.partition("=")
        if role in expected:
            raise ValueError(f"the {role} digest output is repeated")
        expected[role] = _checked(_SHA256, digest, f"{role} digest")
    if set(expected) != set(names):
        raise ValueError(f"digest outputs are {sorted(expected)}, expected {sorted(names)}")
    entries = {entry.name: entry for entry in incoming.iterdir()}
    if set(entries) != set(names.values()):
        raise ValueError(f"artefact holds {sorted(entries)}, expected {sorted(names.values())}")
    for entry in entries.values():
        if entry.is_symlink() or not entry.is_file():
            raise ValueError(f"{entry.name} is not a regular file")
    kept: list[Path] = []
    for role, name in names.items():
        own, received = dist / name, entries[name]
        if role.endswith("-sbom"):
            with received.open("rb") as source, own.open("xb") as copy:  # NOSONAR
                shutil.copyfileobj(source, copy)
            received = own
        elif _sha256(own) != expected[role]:
            raise ValueError(f"{name} from the admit job differs from this job's own")
        if _sha256(received) != expected[role]:
            raise ValueError(f"{name} does not match the admit job's digest")
        kept.append(own)
    return tuple(kept)


def _trusted_bases() -> list[Path]:
    bases = [Path.cwd(), Path(tempfile.gettempdir())]
    bases += [Path(os.environ[name]) for name in _RUNNER_DIRECTORIES if os.environ.get(name)]
    return [base.resolve() for base in bases]


def _confined(path: Path) -> Path:
    """The resolved path argument, once it is inside a trusted directory.

    Resolving follows `..` and symbolic links, so a link that is itself the
    argument is refused first; the commands' own link checks would otherwise
    see only its target.
    """
    if path.is_symlink():
        raise ValueError(f"{path} must not be a symbolic link")
    resolved = path.resolve()
    for base in _trusted_bases():
        if resolved.is_relative_to(base):
            return resolved
    raise ValueError(f"{path} is outside the working, workspace and temporary directories")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, *required: str, paths: tuple[str, ...] = (),
                optional: tuple[str, ...] = (), data: bool = False) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        for option in required:
            sub.add_argument(option, required=True)
        for option in paths:
            sub.add_argument(option, type=Path, required=True)
        for option in optional:
            sub.add_argument(option, type=Path)
        if data:
            sub.add_argument("--data-files", default="")
        return sub

    command("cargo-package", "--version", "--binary", paths=("--manifest",))
    command("toolchain", paths=("--source", "--root"))
    command("package", "--binary", "--target", "--stem", "--version", "--epoch",
            paths=("--executables", "--source", "--root", "--output-dir"), data=True)
    command("verify", "--binary", "--target", paths=("--archive",), optional=("--extract-dir",),
            data=True)
    command("receive", "--stem", "--version", "--binary", "--target",
            paths=("--incoming", "--dist", "--extract-dir", "--source", "--root"), data=True)
    command("smoke", "--version", paths=("--executable",))
    command("linkage", "--target", paths=("--readobj-output",))
    command("macho-linkage", "--target", paths=("--executable",))
    command("sbom-lists", "--package", paths=("--sbom",))
    command("digests", "--stem", "--version", paths=("--dist",))
    command("assets", "--stem", "--version")
    command("accept", "--stem", "--version", paths=("--incoming", "--dist")).add_argument(
        "--digest", action="append", required=True)
    return parser


def _run(arguments: argparse.Namespace) -> str:
    match arguments.command:
        case "cargo-package":
            return cargo_package(_confined(arguments.manifest), arguments.version,
                                 arguments.binary)
        case "toolchain":
            return toolchain(_confined(arguments.source), _confined(arguments.root))
        case "package":
            if not arguments.epoch.isdigit():
                raise ValueError(f"epoch is not a Unix time: {arguments.epoch!r}")
            return str(package(
                executables=_confined(arguments.executables), binary=arguments.binary,
                triple=arguments.target, stem=arguments.stem, version=arguments.version,
                source=_confined(arguments.source), root=_confined(arguments.root),
                epoch=int(arguments.epoch), output_dir=_confined(arguments.output_dir),
                data_files=arguments.data_files,
            ))
        case "verify":
            extract_dir = arguments.extract_dir
            return "\n".join(verify(
                archive=_confined(arguments.archive), binary=arguments.binary,
                triple=arguments.target,
                extract_dir=None if extract_dir is None else _confined(extract_dir),
                data_files=arguments.data_files,
            ))
        case "receive":
            return str(receive(
                incoming=_confined(arguments.incoming), stem=arguments.stem,
                version=arguments.version, binary=arguments.binary, triple=arguments.target,
                dist=_confined(arguments.dist), extract_dir=_confined(arguments.extract_dir),
                source=_confined(arguments.source), root=_confined(arguments.root),
                data_files=arguments.data_files,
            ))
        case "smoke":
            return smoke(_confined(arguments.executable), arguments.version)
        case "linkage":
            output = _confined(arguments.readobj_output).read_bytes().decode("utf-8")
            return "\n".join(linkage(output, arguments.target))
        case "macho-linkage":
            executable = _confined(arguments.executable).read_bytes()
            return "\n".join(macho_linkage(executable, arguments.target))
        case "digests":
            return "\n".join(digests(_confined(arguments.dist), arguments.stem, arguments.version))
        case "assets":
            return "\n".join(handoff_names(arguments.stem, arguments.version).values())
        case "accept":
            return "\n".join(path.name for path in accept(
                incoming=_confined(arguments.incoming), dist=_confined(arguments.dist),
                stem=arguments.stem, version=arguments.version, digest_outputs=arguments.digest,
            ))
        case _:
            if not sbom_lists(_confined(arguments.sbom), arguments.package):
                raise ValueError(f"{arguments.sbom} does not list the package {arguments.package}")
            return f"{arguments.sbom.name} lists {arguments.package}"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        print(_run(arguments))
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"binary_release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
