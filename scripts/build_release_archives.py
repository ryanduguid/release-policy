"""Build deterministic source-release archives from tracked Git content."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from git_revision import resolve_commit
from python_release import validate_source_directory

_ARCHIVE_FORMATS = (("zip", ".zip"), ("tar.gz", ".tar.gz"))
_GIT_CONFIG = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.eol=lf",
)
_SAFE_PREFIX = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/\Z")


def _validate_prefix(prefix: str) -> None:
    parts = prefix[:-1].split("/") if prefix.endswith("/") else []
    if (
        not _SAFE_PREFIX.fullmatch(prefix)
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(
            "prefix must be a safe relative POSIX path ending in '/'"
        )


def _preflight_outputs(output_base: Path, repository: Path, *, relative: bool) -> tuple[Path, ...]:
    # The caller owns the filesystem. These checks prevent validation side effects;
    # they do not provide atomic protection against concurrent directory replacement.
    if ".." in output_base.parts:
        raise ValueError("output directory must not contain parent traversal")
    parent = output_base.parent
    for component in (parent, *parent.parents):
        if component.is_symlink():
            raise ValueError("output directory must not traverse symbolic links")
    resolved_parent = parent.resolve()
    if resolved_parent != parent:
        raise ValueError("output directory must not traverse symbolic links")
    if relative and resolved_parent != repository and repository not in resolved_parent.parents:
        raise ValueError("relative output directory must remain inside the repository")
    outputs = tuple(Path(f"{output_base}{suffix}") for _, suffix in _ARCHIVE_FORMATS)
    for output in outputs:
        if output.is_symlink() or output.exists():
            raise FileExistsError(f"refusing to overwrite existing archive: {output}")
    return outputs


def build_release_archives(
    *,
    commit: str,
    prefix: str,
    output_base: Path,
    source_directory: str = ".",
    cwd: Path | None = None,
) -> tuple[Path, ...]:
    """Build ZIP and tar.gz archives with stable text and time metadata."""

    _validate_prefix(prefix)
    repository = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    commit = resolve_commit(repository, commit)
    source_directory = validate_source_directory(repository, source_directory)
    treeish = commit if source_directory == "." else f"{commit}:{source_directory}"
    if source_directory != ".":
        selected = subprocess.run(
            ["git", "cat-file", "-t", treeish], cwd=repository,
            check=True, capture_output=True, text=True,
        )
        if selected.stdout.strip() != "tree":
            raise ValueError("source-directory must be a tree at the selected commit")
    supplied_output_base = Path(output_base)
    output_base = (
        supplied_output_base
        if supplied_output_base.is_absolute()
        else repository / supplied_output_base
    )
    output_base = output_base.absolute()
    relative = not supplied_output_base.is_absolute()
    outputs = _preflight_outputs(output_base, repository, relative=relative)

    environment = os.environ.copy()
    environment["TZ"] = "UTC"
    timestamp_result = subprocess.run(
        ("git", "show", "-s", "--format=%ct", commit),
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    commit_timestamp = timestamp_result.stdout.strip()
    if re.fullmatch(r"[0-9]+", commit_timestamp) is None:
        raise ValueError("commit timestamp must be a non-negative integer")

    output_base.parent.mkdir(parents=True, exist_ok=True)
    _preflight_outputs(output_base, repository, relative=relative)
    try:
        for (archive_format, _), output in zip(
            _ARCHIVE_FORMATS,
            outputs,
            strict=True,
        ):
            subprocess.run(
                (
                    "git",
                    *_GIT_CONFIG,
                    "archive",
                    f"--format={archive_format}",
                    f"--prefix={prefix}",
                    f"--mtime=@{commit_timestamp}",
                    f"--output={output.resolve()}",
                    treeish,
                ),
                cwd=repository,
                env=environment,
                check=True,
            )
    except BaseException:
        for output in outputs:
            output.unlink(missing_ok=True)
        raise

    return outputs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build reproducible Git source archives.",
    )
    parser.add_argument("--commit", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--source-directory", default=".")
    parser.add_argument("--output-base", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    build_release_archives(
        commit=args.commit,
        prefix=args.prefix,
        source_directory=args.source_directory,
        output_base=args.output_base,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
