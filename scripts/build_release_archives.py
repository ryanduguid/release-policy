"""Build deterministic source-release archives from tracked Git content."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
from typing import Sequence

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


def build_release_archives(
    *,
    commit: str,
    prefix: str,
    output_base: Path,
    source_directory: str = ".",
    cwd: Path | None = None,
) -> tuple[Path, Path]:
    """Build ZIP and tar.gz archives with stable text and time metadata."""

    _validate_prefix(prefix)
    if not commit or any(character.isspace() for character in commit):
        raise ValueError("commit must be a non-empty Git revision without whitespace")

    repository = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    source_directory = validate_source_directory(repository, source_directory)
    treeish = commit if source_directory == "." else f"{commit}:{source_directory}"
    supplied_output_base = Path(output_base)
    output_base = (
        supplied_output_base
        if supplied_output_base.is_absolute()
        else repository / supplied_output_base
    )
    lexical_parent = output_base.parent.absolute()
    if lexical_parent.is_symlink():
        raise ValueError("output directory must not be a symbolic link")
    lexical_parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = lexical_parent.resolve()
    if resolved_parent != lexical_parent:
        raise ValueError("output directory must not traverse symbolic links")
    if (
        not supplied_output_base.is_absolute()
        and resolved_parent != repository
        and repository not in resolved_parent.parents
    ):
        raise ValueError("relative output directory must remain inside the repository")

    outputs = tuple(Path(f"{output_base}{suffix}") for _, suffix in _ARCHIVE_FORMATS)
    for output in outputs:
        if output.exists():
            raise FileExistsError(f"refusing to overwrite existing archive: {output}")

    environment = os.environ.copy()
    environment["TZ"] = "UTC"
    timestamp_result = subprocess.run(
        ("git", "show", "-s", "--format=%ct", f"{commit}^{{commit}}"),
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    commit_timestamp = timestamp_result.stdout.strip()
    if re.fullmatch(r"[0-9]+", commit_timestamp) is None:
        raise ValueError("commit timestamp must be a non-negative integer")

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
