# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

"""Bundle skill upload ZIPs into one deterministic ZIP release asset."""

from __future__ import annotations

import argparse
import time
import zipfile
from pathlib import Path
from typing import Sequence


def build_skill_bundle(*, output: Path, archives: Sequence[Path], mtime: int) -> None:
    """Store each archive at the bundle's top, in byte order of its name.

    Every entry gets the same time, mode and creator system, and is stored
    without compression, so the bytes depend only on the inputs and mtime.
    """

    date_time = time.gmtime(mtime)[:6]
    # "x" refuses an existing output, as the source archive builder does.
    with zipfile.ZipFile(output, "x") as bundle:
        for archive in sorted(archives, key=lambda path: path.name.encode()):
            entry = zipfile.ZipInfo(archive.name, date_time)
            # Unix, whatever the host; zipfile writes 0 on Windows.
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, archive.read_bytes())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mtime", required=True, type=int, help="entry time, in Unix seconds")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("archives", nargs="+", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    build_skill_bundle(output=args.output, archives=args.archives, mtime=args.mtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
