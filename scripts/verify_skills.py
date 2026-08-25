from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


SUPPORTED_MODES = frozenset({"subcontractor-accounting-v1"})
REGULAR_GIT_MODES = frozenset({"100644", "100755"})
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
Command = tuple[str, ...]
Runner = Callable[..., subprocess.CompletedProcess[object]]


class VerificationError(ValueError):
    """A skill pack does not satisfy the closed verification contract."""


def require_supported_mode(mode: str) -> None:
    if mode not in SUPPORTED_MODES:
        raise VerificationError(f"unsupported skills verification mode: {mode!r}")


def guard_skill_release(mode: str, tag: str) -> None:
    require_supported_mode(mode)
    if mode == "subcontractor-accounting-v1" and tag == "v0.1.0":
        raise VerificationError("v0.1.0 is frozen and must never be rebuilt or replaced")


def resolve_safe_relative(
    root: Path,
    supplied: str,
    *,
    label: str,
) -> tuple[PurePosixPath, Path]:
    raw_parts = supplied.split("/")
    if (
        not supplied
        or "\\" in supplied
        or re.match(r"^[A-Za-z]:/", supplied)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise VerificationError(f"{label} must be a safe relative POSIX path")
    relative = PurePosixPath(supplied)
    if relative.is_absolute():
        raise VerificationError(f"{label} must be a safe relative POSIX path")
    resolved_root = root.resolve(strict=True)
    cursor = resolved_root
    for part in relative.parts:
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError as error:
            raise VerificationError(f"{label} is missing: {supplied}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError(f"{label} contains a symlink component: {supplied}")
    candidate = cursor.resolve(strict=True)
    if candidate == resolved_root or resolved_root not in candidate.parents:
        raise VerificationError(f"{label} resolves outside the consumer checkout")
    return relative, candidate


def tracked_mode(root: Path, relative: PurePosixPath) -> str:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--error-unmatch", "--", relative.as_posix()],
        cwd=root,
        check=False,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 1:
        raise VerificationError(f"required path is not one tracked Git entry: {relative}")
    return lines[0].split(maxsplit=1)[0]


def require_tracked_regular_file(root: Path, supplied: str, *, label: str) -> Path:
    relative, candidate = resolve_safe_relative(root, supplied, label=label)
    if not candidate.is_file():
        raise VerificationError(f"{label} is not a regular file: {supplied}")
    mode = tracked_mode(root, relative)
    if mode not in REGULAR_GIT_MODES:
        raise VerificationError(f"{label} has forbidden Git mode {mode}: {supplied}")
    return candidate


def require_tracked_test_files(root: Path) -> tuple[Path, ...]:
    relative, tests_root = resolve_safe_relative(root, "tests", label="tests directory")
    if not tests_root.is_dir():
        raise VerificationError("tests must be a real directory")
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", relative.as_posix()],
        cwd=root,
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
    )
    names = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    tests = tuple(
        require_tracked_regular_file(root, name, label="test file")
        for name in names
        if PurePosixPath(name).name.startswith("test")
        and PurePosixPath(name).suffix == ".py"
    )
    if not tests:
        raise VerificationError("tests must contain a tracked regular test*.py file")
    return tests


def read_canonical_version(path: Path) -> str:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise VerificationError("version file must be UTF-8") from error
    if text.startswith("\ufeff"):
        raise VerificationError("version file must not contain a BOM")
    match = re.fullmatch(r"(.+?)(?:\r?\n)?", text)
    if match is None or SEMVER.fullmatch(match.group(1)) is None:
        raise VerificationError("version file must contain one canonical MAJOR.MINOR.PATCH line")
    return match.group(1)


def commands_for_mode(mode: str, *, python: str = sys.executable) -> tuple[Command, ...]:
    require_supported_mode(mode)
    return (
        (python, "-m", "pip", "install", "--isolated", "--disable-pip-version-check", "--no-input", "--no-deps", "--requirement", "requirements-test.txt"),
        (python, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"),
        (python, "scripts/validate_validation.py"),
        (python, "tests/verify_skills_cli.py"),
    )


def verify_skill_pack(
    root: Path,
    mode: str,
    version_file: str,
    *,
    runner: Runner = subprocess.run,
) -> None:
    require_supported_mode(mode)
    version_path = require_tracked_regular_file(root, version_file, label="version file")
    require_tracked_regular_file(root, "requirements-test.txt", label="requirements file")
    require_tracked_regular_file(root, "scripts/validate_validation.py", label="validation program")
    require_tracked_regular_file(root, "tests/verify_skills_cli.py", label="Skills CLI program")
    require_tracked_test_files(root)
    read_canonical_version(version_path)
    for command in commands_for_mode(mode):
        runner(command, cwd=root, check=True, shell=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a closed skill-pack release mode.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    guard = subcommands.add_parser("guard-release")
    guard.add_argument("--mode", required=True)
    guard.add_argument("--tag", required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--mode", required=True)
    verify.add_argument("--version-file", default="VERSION")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "guard-release":
            guard_skill_release(arguments.mode, arguments.tag)
        else:
            verify_skill_pack(Path.cwd(), arguments.mode, arguments.version_file)
    except (VerificationError, subprocess.CalledProcessError) as error:
        print(f"verify_skills: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
