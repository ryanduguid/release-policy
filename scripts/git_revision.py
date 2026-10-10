"""Resolve caller-supplied revisions to immutable Git commit identities."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def resolve_commit(root: Path, revision: str) -> str:
    """Peel one revision safely; subsequent Git operations use only the full SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    sha = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None:
        raise ValueError(f"revision is not a commit in this checkout: {revision}")
    return sha
