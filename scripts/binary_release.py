"""Package, check and validate the per-target archives of a Rust binary release.

The binary release family (`release-binary.yml`) builds each target in its own
read-only job and publishes from one privileged job that never runs consumer
code. The build jobs call `package`, `smoke` and `linkage`; the publisher calls
`cargo-package` before anything is built and `verify` on each archive it
receives, so a missing, extra or unsafe member stops the release.

Every output path comes from the workflow or build_binary.sh, under the
runner's temporary directory or the checkout's dist directory; member names
are checked before anything is extracted.
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
import time
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Sequence, cast

_STEM = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_BINARY = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


@dataclass(frozen=True)
class Target:
    triple: str
    suffix: str
    executable_suffix: str
    # llvm-readobj's Format line for an x86-64 executable of this target.
    object_format: str


TARGETS = {
    target.triple: target
    for target in (
        Target("x86_64-unknown-linux-musl", ".tar.gz", "", "elf64-x86-64"),
        Target("x86_64-pc-windows-msvc", ".zip", ".exe", "COFF-x86-64"),
    )
}
# An archive holds at most the executable and 3 documents. The size bounds
# are far above any expected release. They apply to the archive file, to
# each member and to everything decompressed, so neither a member nor the
# archive's own metadata (PAX headers, a ZIP central directory) can make
# the publisher read without limit.
MAX_MEMBERS = 4
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_MEMBER_BYTES
MAX_DECOMPRESSED_BYTES = MAX_MEMBER_BYTES + 64 * 1024
_MAX_DIGEST_BYTES = 1024
_ZIP_END = b"PK\x05\x06"
_ZIP_END_SEARCH = 22 + 0xFFFF
_MAX_ZIP_DIRECTORY_BYTES = 64 * 1024
_TOOLCHAIN_CHANNEL = re.compile(r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_DIGEST_LINE = re.compile(r"(?P<digest>[0-9a-f]{64}) [ *](?P<name>[^\r\n]+)\r?\n\Z")
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
# API set names, which Rust's standard library imports, are resolved by the
# Windows loader and have no file of that name in System32.
_API_SET = re.compile(r"(?:api|ext)-ms-win-[a-z0-9-]+\.dll", re.IGNORECASE)
_NEEDED = re.compile(r"NeededLibraries \[\n(?P<names>(?:[^\]\n]*\n)*?)\]", re.MULTILINE)


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


def member_names(binary: str, triple: str, documents: Sequence[str]) -> tuple[str, ...]:
    executable = _checked(_BINARY, binary, "binary") + _target(triple).executable_suffix
    return (executable, *sorted(documents))


def cargo_package(manifest: Path, release_version: str, binary: str) -> str:
    """The package name, once the manifest agrees with the release.

    The literal `[package] version` must equal the tracked VERSION, and the
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
    if _checked(_BINARY, binary, "binary") not in declared | {name}:
        raise ValueError(f"{binary} is neither the package {name} nor a declared [[bin]]")
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
    *, executable: Path, binary: str, triple: str, stem: str, version: str,
    source: Path, root: Path, epoch: int, output_dir: Path,
) -> Path:
    target = _target(triple)
    output = output_dir / asset_name(stem, version, triple)
    documents = find_documents(source, root)
    names = member_names(binary, triple, tuple(documents))
    members = [(names[0], executable.read_bytes(), 0o755)]
    members += [(name, documents[name].read_bytes(), 0o644) for name in names[1:]]
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
        handle.seek(max(0, size - _ZIP_END_SEARCH))
        tail = handle.read()
    end = tail.rfind(_ZIP_END)
    if end < 0 or len(tail) - end < 22:
        raise ValueError(f"{archive.name} has no ZIP end of central directory record")
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


def verify(*, archive: Path, binary: str, triple: str, extract_dir: Path | None) -> tuple[str, ...]:
    """Require exactly the release members, and optionally extract them."""
    target = _target(triple)
    members = _archive_members(archive, target)
    documents = sorted(set(members) - {binary + target.executable_suffix})
    expected = member_names(binary, triple, documents)
    for name in members:
        path = PurePosixPath(name)
        if len(path.parts) != 1 or name in {".", ".."} or "\\" in name:
            raise ValueError(f"unsafe member name {name!r}")
    if expected[0] not in members:
        raise ValueError(f"the executable {expected[0]} is missing")
    if set(documents) - set(DOCUMENTS) or not REQUIRED_DOCUMENTS <= set(documents):
        raise ValueError(f"members are {sorted(members)}, expected {list(expected)}")
    if target.executable_suffix == "" and not members[expected[0]][1] & 0o100:
        raise ValueError(f"{expected[0]} is not executable")
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


def linkage(readobj_output: str, triple: str, system_directory: Path | None) -> tuple[str, ...]:
    target = _target(triple)
    lines = {line.strip() for line in readobj_output.splitlines()}
    if not {f"Format: {target.object_format}", "Arch: x86_64"} <= lines:
        raise ValueError(f"not an x86-64 {target.object_format} executable")
    needed = needed_libraries(readobj_output)
    if target.executable_suffix == "":
        if needed or "PT_INTERP" in readobj_output:
            raise ValueError(f"not a static executable: needs {list(needed)} or an interpreter")
        return needed
    runtime = [name for name in needed if _WINDOWS_RUNTIME.fullmatch(name)]
    if runtime:
        raise ValueError(f"imports C runtime DLLs: {runtime}")
    if system_directory is None:
        raise ValueError("a Windows linkage check needs --system-directory")
    unsafe = [name for name in needed if not _DLL_NAME.fullmatch(name)]
    if unsafe:
        raise ValueError(f"imports DLLs by path or with an unexpected name: {unsafe}")
    foreign = [
        name for name in needed
        if not _API_SET.fullmatch(name) and not (system_directory / name).is_file()
    ]
    if foreign:
        raise ValueError(f"imports DLLs Windows does not ship: {foreign}")
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
    dist: Path, extract_dir: Path,
) -> Path:
    """Admit one build job's artefact into the release, or refuse it.

    The artefact must hold exactly the archive and its digest line, the
    digest must match the bytes, and the archive must pass `verify`. The
    digest only proves the transfer: the build job wrote both files.
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
    with archive.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != match.group("digest"):
        raise ValueError(f"{name} does not match its recorded digest")
    verify(archive=archive, binary=binary, triple=triple, extract_dir=extract_dir)
    dist.mkdir(parents=True, exist_ok=True)
    admitted = dist / name
    with archive.open("rb") as source, admitted.open("xb") as handle:  # NOSONAR
        shutil.copyfileobj(source, handle)
    return admitted


def sbom_lists(sbom: Path, package_name: str) -> bool:
    document = json.loads(sbom.read_bytes().decode("utf-8"))
    packages = document.get("packages") if isinstance(document, dict) else None
    return isinstance(packages, list) and any(
        isinstance(item, dict) and item.get("name") == package_name for item in packages
    )




def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, *required: str, paths: tuple[str, ...] = (),
                optional: tuple[str, ...] = ()) -> None:
        sub = commands.add_parser(name)
        for option in required:
            sub.add_argument(option, required=True)
        for option in paths:
            sub.add_argument(option, type=Path, required=True)
        for option in optional:
            sub.add_argument(option, type=Path)

    command("cargo-package", "--version", "--binary", paths=("--manifest",))
    command("toolchain", paths=("--source", "--root"))
    command("package", "--binary", "--target", "--stem", "--version", "--epoch",
            paths=("--executable", "--source", "--root", "--output-dir"))
    command("verify", "--binary", "--target", paths=("--archive",), optional=("--extract-dir",))
    command("receive", "--stem", "--version", "--binary", "--target",
            paths=("--incoming", "--dist", "--extract-dir"))
    command("smoke", "--version", paths=("--executable",))
    command("linkage", "--target", paths=("--readobj-output",), optional=("--system-directory",))
    command("sbom-lists", "--package", paths=("--sbom",))
    return parser


def _run(arguments: argparse.Namespace) -> str:
    match arguments.command:
        case "cargo-package":
            return cargo_package(arguments.manifest, arguments.version, arguments.binary)
        case "toolchain":
            return toolchain(arguments.source, arguments.root)
        case "package":
            if not arguments.epoch.isdigit():
                raise ValueError(f"epoch is not a Unix time: {arguments.epoch!r}")
            return str(package(
                executable=arguments.executable, binary=arguments.binary,
                triple=arguments.target, stem=arguments.stem, version=arguments.version,
                source=arguments.source, root=arguments.root, epoch=int(arguments.epoch),
                output_dir=arguments.output_dir,
            ))
        case "verify":
            return "\n".join(verify(
                archive=arguments.archive, binary=arguments.binary, triple=arguments.target,
                extract_dir=arguments.extract_dir,
            ))
        case "receive":
            return str(receive(
                incoming=arguments.incoming, stem=arguments.stem, version=arguments.version,
                binary=arguments.binary, triple=arguments.target, dist=arguments.dist,
                extract_dir=arguments.extract_dir,
            ))
        case "smoke":
            return smoke(arguments.executable, arguments.version)
        case "linkage":
            output = arguments.readobj_output.read_bytes().decode("utf-8")
            return "\n".join(linkage(output, arguments.target, arguments.system_directory))
        case _:
            if not sbom_lists(arguments.sbom, arguments.package):
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
