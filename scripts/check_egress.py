"""Check a publication candidate for content that must not leave private hands.

The gate runs on egress: at the point files are about to be published, never
as a pre-commit hook. A hook on the way into a private repository would block
authoring the very material this exists to contain, so the rule is that
private content is written freely and checked once, on the way out.

Three layers, borrowed from the privacy-gradient pattern and kept deliberately
small:

  A  configured terms, hard fail. Employer names, client identifiers, billing
     codes: the words that must never reach a public artefact. They are
     supplied by the operator in a terms file that is never committed, because
     a public repository carrying the list of words it must not publish has
     already published them.
  B  structural patterns, soft fail. Absolute user paths, private hostnames
     and addresses, bucket URIs, dotenv references: shapes that usually mean a
     private detail travelled with the file. These pass only with an explicit
     --allow-structural, so a real one is waved through by a person rather
     than by silence.
  C  the reserved namespace x-internal-, hard fail. Anything internal is
     marked with it at the point it is written, and its presence in a
     publication candidate is decisive on its own.

A file that describes this check carries the shapes it looks for, as does this
module. --exclude is for those, and it is bounded on both sides: every excluded
file is printed, and a pattern that matches nothing is an error rather than a
no-op, so a stale exclusion cannot sit there protecting nothing.

The exit status is a three-state contract:

    0   nothing to report
    1   a finding: do not publish
    2   the check did not run, so nothing was checked

The third state is what stops a broken gate reading as a clean one. A missing
terms file, an unreadable candidate or a bad regular expression all land in
the 2 band and say so. Fail closed: without either --terms or an explicit
--no-terms, layer A cannot run and the check refuses to report a verdict.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_COULD_NOT_RUN = 2

#: Anything internal carries this prefix where it is written; a publication
#: candidate that still holds one is refused whatever else it says.
RESERVED_NAMESPACE = "x-internal-"

#: Layer B. Shapes, not secrets, so these live in the open safely. Each one is
#: a pattern that has meant a private detail travelled with a published file.
STRUCTURAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("windows user path", r"[A-Za-z]:\\+Users\\+[^\\\s\"']+"),
    ("unix home path", r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    ("private ipv4", r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ("private ipv4", r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    ("localhost url", r"\bhttps?://(?:localhost|127\.0\.0\.1)(?::\d+)?\b"),
    ("internal hostname", r"\b[A-Za-z0-9-]+\.(?:internal|local|lan|corp|intranet)\b"),
    ("object store uri", r"\b(?:s3|gs|azure)://[A-Za-z0-9._-]+"),
    ("dotenv reference", r"(?<![\w.-])\.env(?:\.[A-Za-z0-9_-]+)?\b"),
    ("unc share", r"\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9._$-]+"),
)

#: Read as text and scanned. Everything else is reported as unscanned rather
#: than passed silently: a gate cannot vouch for bytes it never decoded.
TEXT_SUFFIXES = frozenset({
    ".cfg", ".cff", ".csv", ".html", ".ini", ".json", ".jsonl", ".md", ".py",
    ".rst", ".sh", ".toml", ".ts", ".tsv", ".txt", ".xml", ".yaml", ".yml",
})


class EgressError(Exception):
    """The check could not be run. Never raised for a finding."""


@dataclass(frozen=True)
class Finding:
    layer: str
    path: str
    line: int
    label: str
    excerpt: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.layer}] {self.label}: {self.excerpt}"


def load_terms(path: Path) -> tuple[str, ...]:
    """Read the operator's layer A terms: one per line, # starts a comment.

    A terms file that exists but holds no term is an error, not an empty
    policy. The likeliest cause is a file that was emptied or truncated, and
    treating that as "nothing to check for" is the silent pass this gate
    exists to prevent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise EgressError(f"cannot read the terms file {path}: {error}") from error
    terms = tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not terms:
        raise EgressError(f"the terms file {path} lists no terms")
    return terms


def candidate_files(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Every file under the supplied paths, in a stable order."""
    found: list[Path] = []
    for path in paths:
        if not path.exists():
            raise EgressError(f"publication candidate {path} does not exist")
        if path.is_file():
            found.append(path)
            continue
        found.extend(child for child in path.rglob("*") if child.is_file())
    return tuple(sorted(set(found)))


def partition_excluded(
    files: Sequence[Path],
    patterns: Sequence[str],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Split the candidate into the files to scan and the files excluded.

    A document that describes this check necessarily contains the shapes it
    looks for, and so does the check's own source. Those are the honest use
    for an exclusion, and the two rules around it are what keep an exclusion
    from quietly becoming a hole: every excluded file is printed, and a
    pattern that matches nothing is an error rather than a no-op. A stale
    exclusion protects nothing and hides the next file that needs looking at.
    """
    kept: list[Path] = []
    excluded: list[Path] = []
    unused = set(patterns)
    for path in files:
        posix = path.as_posix()
        matched = [p for p in patterns if PurePath(posix).match(p) or posix.endswith("/" + p)]
        if matched:
            unused.difference_update(matched)
            excluded.append(path)
        else:
            kept.append(path)
    if unused:
        raise EgressError(
            "these --exclude pattern(s) matched no file in the candidate: "
            + ", ".join(sorted(unused))
        )
    return tuple(kept), tuple(excluded)


def _read_text(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    except OSError as error:
        raise EgressError(f"cannot read the publication candidate {path}: {error}") from error


def _excerpt(line: str, start: int, end: int, *, width: int = 24) -> str:
    """The match with a little context, so a finding can be located by eye.

    The finding is printed where the operator can already read the file, so
    the excerpt is bounded: a layer A hit should not reprint the paragraph it
    was found in to whatever CI log this runs under.
    """
    left = max(0, start - width)
    right = min(len(line), end + width)
    prefix = "..." if left > 0 else ""
    suffix = "..." if right < len(line) else ""
    return f"{prefix}{line[left:right].strip()}{suffix}"


def scan_text(
    text: str,
    *,
    label_path: str,
    terms: Iterable[str],
    patterns: Iterable[tuple[str, str]] = STRUCTURAL_PATTERNS,
) -> tuple[Finding, ...]:
    """Apply all three layers to one file's text."""
    findings: list[Finding] = []
    lowered_terms = [(term, term.lower()) for term in terms]
    compiled = []
    for label, pattern in patterns:
        try:
            compiled.append((label, re.compile(pattern, re.IGNORECASE)))
        except re.error as error:
            raise EgressError(f"structural pattern {label!r} does not compile: {error}") from error

    for number, line in enumerate(text.splitlines(), start=1):
        lowered_line = line.lower()
        for term, lowered_term in lowered_terms:
            start = lowered_line.find(lowered_term)
            if start >= 0:
                findings.append(Finding(
                    layer="A", path=label_path, line=number, label=f"configured term {term!r}",
                    excerpt=_excerpt(line, start, start + len(term)),
                ))
        for label, expression in compiled:
            match = expression.search(line)
            if match:
                findings.append(Finding(
                    layer="B", path=label_path, line=number, label=label,
                    excerpt=_excerpt(line, match.start(), match.end()),
                ))
        marker = lowered_line.find(RESERVED_NAMESPACE)
        if marker >= 0:
            findings.append(Finding(
                layer="C", path=label_path, line=number,
                label=f"reserved namespace {RESERVED_NAMESPACE!r}",
                excerpt=_excerpt(line, marker, marker + len(RESERVED_NAMESPACE)),
            ))
    return tuple(findings)


def check_candidate(
    paths: Sequence[Path],
    *,
    terms: Sequence[str],
    exclude: Sequence[str] = (),
) -> tuple[tuple[Finding, ...], tuple[Path, ...], tuple[Path, ...]]:
    """Return the findings, the files not scanned as text, and the excluded."""
    findings: list[Finding] = []
    unscanned: list[Path] = []
    scanned, excluded = partition_excluded(candidate_files(paths), exclude)
    for path in scanned:
        text = _read_text(path)
        if text is None:
            unscanned.append(path)
            continue
        findings.extend(scan_text(text, label_path=path.as_posix(), terms=terms))
    return tuple(findings), tuple(unscanned), excluded


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check a publication candidate before it leaves private hands.",
    )
    parser.add_argument(
        "paths", nargs="+", type=Path,
        help="files or directories about to be published",
    )
    parser.add_argument(
        "--terms", type=Path,
        help="layer A terms file, one term per line; never commit it",
    )
    parser.add_argument(
        "--no-terms", action="store_true",
        help="run without layer A, acknowledging that only B and C are checked",
    )
    parser.add_argument(
        "--allow-structural", action="store_true",
        help="report layer B findings without failing on them",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help=(
            "skip a file that necessarily carries these shapes, such as this "
            "check's own documentation; every exclusion is printed, and one "
            "that matches nothing is an error"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.terms and arguments.no_terms:
            raise EgressError("--terms and --no-terms contradict each other")
        if not arguments.terms and not arguments.no_terms:
            raise EgressError(
                "layer A needs a terms file. Pass --terms <file>, or --no-terms to "
                "publish with only the structural and reserved-namespace layers "
                "checked. Refusing to imply a clean result from an absent policy"
            )
        terms = load_terms(arguments.terms) if arguments.terms else ()
        findings, unscanned, excluded = check_candidate(
            arguments.paths, terms=terms, exclude=arguments.exclude,
        )
    except EgressError as error:
        print(f"check_egress: EGRESS CHECK ERROR: {error}", file=sys.stderr)
        print("check_egress: nothing was checked; this candidate is unchecked, not clean")
        return EXIT_COULD_NOT_RUN

    for path in excluded:
        print(f"check_egress: excluded by --exclude, not checked: {path.as_posix()}")
    for path in unscanned:
        print(f"check_egress: not scanned as text: {path.as_posix()}")
    for finding in findings:
        print(f"check_egress: {finding.render()}")

    blocking = [f for f in findings if f.layer != "B" or not arguments.allow_structural]
    if blocking:
        print(f"check_egress: RESULT: {len(blocking)} finding(s); do not publish")
        return EXIT_FINDINGS
    if findings:
        print(
            f"check_egress: RESULT: clean, with {len(findings)} structural finding(s) "
            "waved through by --allow-structural"
        )
    else:
        print("check_egress: RESULT: clean")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
