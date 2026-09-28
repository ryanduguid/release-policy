"""Name the pin families a release-policy change moves, so consumers re-pin only those.

Usage: python scripts/pin_families.py OLD_COMMIT NEW_COMMIT

Consumers pin a reusable workflow by commit. A family is that workflow and every file
it executes, found by following references from it: the workflows it calls, the
composite action it uses, and the scripts it names, directly or through the scripts it
sources and runs. A family whose files are identical at both commits keeps its pin, so an
attribution-only change moves the attribution pins and leaves every release caller alone,
and a change outside these families moves nothing. Comment lines are skipped, so a workflow
that only mentions another does not tie their pins together; a name in other prose, such
as a docstring, still counts, which errs towards one re-pin too many rather than one missed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = {
    "release-python": ".github/workflows/release-python.yml",
    "release-archive": ".github/workflows/release-archive.yml",
    "release-skills": ".github/workflows/release-skills.yml",
    "verify-skills": ".github/workflows/verify-skills.yml",
    "attribution": ".github/workflows/attribution-policy.yml",
}
WORKFLOW = re.compile(r"\.github/workflows/[\w.-]+\.yml")
ACTION = re.compile(r"\.github/actions/[\w.-]+")
SCRIPT = re.compile(r"\b[\w-]+\.(?:py|sh)\b")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def inputs(entry: str, commit: str, root: Path = ROOT) -> set[str]:
    """Every file the family rooted at `entry` executes at `commit`."""
    tree = set(git(root, "ls-tree", "-r", "--name-only", commit).splitlines())
    scripts = {Path(path).name: path for path in tree if path.startswith("scripts/")}
    found: set[str] = set()
    queue = [entry]
    while queue:
        path = queue.pop()
        if path in found or path not in tree:
            continue
        found.add(path)
        text = "\n".join(line for line in git(root, "show", f"{commit}:{path}").splitlines()
                         if not line.lstrip().startswith("#"))
        queue += WORKFLOW.findall(text)
        for action in ACTION.findall(text):
            queue += [member for member in tree if member.startswith(action + "/")]
        queue += [scripts[name] for name in SCRIPT.findall(text) if name in scripts]
    return found


def changed(old: str, new: str, root: Path = ROOT) -> dict[str, list[str]]:
    """For each family, the files it executes that differ between the 2 commits."""
    result = {}
    for family, entry in FAMILIES.items():
        paths = sorted(inputs(entry, old, root) | inputs(entry, new, root))
        # With no paths, git diff would compare the whole tree: a family absent from both moves nothing.
        result[family] = git(root, "diff", "--name-only", old, new, "--", *paths).split() if paths else []
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    for family, paths in changed(argv[0], argv[1]).items():
        print(f"{family}: {'re-pin (' + ', '.join(paths) + ')' if paths else 'keep the pin'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
