"""Check a consumer's review index, verdict files and known-unknowns register.

The rules are those in docs/review-verdicts.md. The check reads the consumer
checkout and its Git history only; it reaches no network and writes nothing.
It stops at the first failure, like verify_skills.py, so a maintainer fixes
one named defect at a time. With --base it also holds the ledgers to their
append-only rule against that revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import takewhile
from pathlib import Path

from verify_skills import VerificationError, require_tracked_regular_file

INDEX_PATH = "reviews/index.json"
KNOWN_UNKNOWNS_PATH = "docs/known-unknowns.md"
VERDICTS = frozenset({"ACCEPT", "REJECT", "FIX"})
CONFIDENCES = frozenset({"HIGH", "MEDIUM", "LOW"})
RELATIONSHIPS = frozenset({"author", "independent"})
ENTRY_KEYS = {
    "confidence",
    "date",
    "id",
    "path",
    "reviewer",
    "scope",
    "sha256",
    "subject",
    "supersedes",
    "verdict",
}
SUBJECT_HEADING = "## Subject"
HEADLINE_HEADING = "## 1. Headline verdict"
CITATION_HEADING = "## 2. Citation audit"
FINDINGS_HEADING = "## 3. Findings"
QUESTIONS_HEADING = "## 4. Open questions"
CHANGES_HEADING = "## 5. Required changes"
METHOD_HEADING = "## 6. Method"
ATTESTATION_HEADING = "## 7. Attestation"
ATTESTATION_SENTENCE = (
    "I read the subject at the commit above and the materials named in section 6."
)
SUBJECT_ROWS = (
    "Repository",
    "Commit",
    "Release",
    "Scope",
    "Reviewer",
    "Relationship",
    "Verdict",
    "Confidence",
    "Date",
)
FINDING_FIELDS = ("Why it matters", "Remedy")
QUESTION_FIELDS = ("Why it matters", "Resolution path")
CHANGE_FIELDS = ("Change", "Re-review")
KNOWN_UNKNOWN_LINES = ("Raised", "Affects", "Why it matters", "Resolution path", "Status")

_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_RELEASE = re.compile(
    r"(?:[a-z0-9]+(?:-[a-z0-9]+)*/)?"
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
# An issuer or Act token, then underscore-joined tokens with no whitespace:
# ITAA_1936_s_109E, SGAA_1992_s_18C(1)(c)(i), ITAA_1997_Subdiv_328-D,
# APES_110_para_R112.1, TPB_PG_01-2023. The convention for each family is
# in the schema document; the grammar only keeps the identifier unambiguous.
_SCOPE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Za-z0-9.()-]+)+\Z")
_FINDING = re.compile(r"(CRITICAL|WARNING|NOTE)\. \S")
_TABLE_ROW = re.compile(r"\|(.*)\|\Z")
_KNOWN_UNKNOWN_HEADING = re.compile(r"## (KU-[0-9]{3}): (.+\?)\Z")
_RESOLVED = re.compile(r"resolved ([0-9]{4}-[0-9]{2}-[0-9]{2}) by (\S.*)\Z")


@dataclass(frozen=True)
class Review:
    id: str
    path: str
    sha256: str
    repository: str
    commit: str
    release: str | None
    scope: tuple[str, ...]
    name: str
    credential: str
    relationship: str
    verdict: str
    confidence: str
    date: str


@dataclass(frozen=True)
class KnownUnknown:
    question: str
    values: tuple[str, ...]
    resolved: tuple[str, str] | None

    @property
    def open(self) -> bool:
        return self.resolved is None


def canonical_text(raw: bytes, *, label: str) -> str:
    """Decode a document the way the baseline digest does: UTF-8, no BOM, LF."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise VerificationError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VerificationError(f"{label} must be valid UTF-8") from error
    if "\r" in text.replace("\r\n", ""):
        raise VerificationError(f"{label} contains a lone carriage return")
    return text.replace("\r\n", "\n")


def _mapping(value: object, *, label: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise VerificationError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"{label} must be a non-empty string")
    return value


def _choice(value: object, *, label: str, allowed: frozenset[str]) -> str:
    result = _text(value, label=label)
    if result not in allowed:
        raise VerificationError(f"{label} must be one of {sorted(allowed)}")
    return result


def _pattern(value: object, *, label: str, pattern: re.Pattern[str], form: str) -> str:
    result = _text(value, label=label)
    if pattern.fullmatch(result) is None:
        raise VerificationError(f"{label} must be {form}")
    return result


def _date(value: object, *, label: str) -> str:
    result = _text(value, label=label)
    try:
        date.fromisoformat(result)
    except ValueError as error:
        raise VerificationError(f"{label} must be an ISO 8601 calendar date") from error
    if len(result) != 10:
        raise VerificationError(f"{label} must be an ISO 8601 calendar date")
    return result


def require_not_after(value: str, bound: str, *, label: str) -> None:
    # Both are YYYY-MM-DD, so string order is calendar order.
    if value > bound:
        raise VerificationError(f"{label} {value} is later than the checked commit date {bound}")


def parse_index(document: object) -> tuple[Review, ...]:
    root = _mapping(document, label="review index", keys={"reviews", "schema"})
    if root["schema"] != 1:
        raise VerificationError("review index schema must be 1")
    entries = root["reviews"]
    if not isinstance(entries, list):
        raise VerificationError("review index reviews must be a list")

    reviews: list[Review] = []
    seen: list[str] = []
    for position, raw_entry in enumerate(entries):
        label = f"review[{position}]"
        entry = _mapping(raw_entry, label=label, keys=ENTRY_KEYS)
        review_id = _pattern(
            entry["id"], label=f"{label}.id", pattern=_ID, form="YYYY-MM-DD-hyphenated-words"
        )
        _date(review_id[:10], label=f"{label}.id date prefix")
        if review_id in seen:
            raise VerificationError(f"duplicate review id: {review_id}")
        label = f"review {review_id}"
        path = _text(entry["path"], label=f"{label} path")
        if path != f"reviews/{review_id}.md":
            raise VerificationError(f"{label} path must be reviews/{review_id}.md")
        digest = _pattern(
            entry["sha256"], label=f"{label} sha256", pattern=_HEX_64, form="64 lower-case hex digits"
        )
        subject = _mapping(
            entry["subject"], label=f"{label} subject", keys={"commit", "release", "repository"}
        )
        repository = _pattern(
            subject["repository"],
            label=f"{label} subject.repository",
            pattern=_REPOSITORY,
            form="owner/name",
        )
        commit = _pattern(
            subject["commit"],
            label=f"{label} subject.commit",
            pattern=_FULL_SHA,
            form="a full lower-case commit SHA",
        )
        release = subject["release"]
        if release is not None:
            release = _pattern(
                release,
                label=f"{label} subject.release",
                pattern=_RELEASE,
                form="a release tag such as v1.2.3 or prefix/v1.2.3",
            )
        scope = entry["scope"]
        if not isinstance(scope, list) or not scope:
            raise VerificationError(f"{label} scope must be a non-empty list")
        provisions = tuple(
            _pattern(
                item,
                label=f"{label} scope[{index}]",
                pattern=_SCOPE,
                form="an identifier such as ITAA_1936_s_109E or APES_110_para_R112.1",
            )
            for index, item in enumerate(scope)
        )
        if len(set(provisions)) != len(provisions):
            raise VerificationError(f"{label} scope repeats an identifier")
        reviewer = _mapping(
            entry["reviewer"],
            label=f"{label} reviewer",
            keys={"credential", "name", "relationship"},
        )
        name = _text(reviewer["name"], label=f"{label} reviewer.name")
        credential = _text(reviewer["credential"], label=f"{label} reviewer.credential")
        relationship = _choice(
            reviewer["relationship"], label=f"{label} reviewer.relationship", allowed=RELATIONSHIPS
        )
        verdict = _choice(entry["verdict"], label=f"{label} verdict", allowed=VERDICTS)
        confidence = _choice(entry["confidence"], label=f"{label} confidence", allowed=CONFIDENCES)
        signed = _date(entry["date"], label=f"{label} date")
        supersedes = entry["supersedes"]
        if supersedes is not None and supersedes not in seen:
            raise VerificationError(f"{label} supersedes must name an earlier listed review")
        seen.append(review_id)
        reviews.append(
            Review(
                review_id,
                path,
                digest,
                repository,
                commit,
                release,
                provisions,
                name,
                credential,
                relationship,
                verdict,
                confidence,
                signed,
            )
        )

    if seen != sorted(seen):
        raise VerificationError("review index must be sorted by id")
    return tuple(reviews)


def load_index_document(raw: bytes, *, label: str) -> object:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise VerificationError(f"{label} must not contain a UTF-8 BOM")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError(f"{label} must be valid UTF-8 JSON") from error
    if raw != (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"):
        raise VerificationError(f"{label} must use canonical sorted JSON")
    return document


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def head_date(root: Path) -> str:
    result = _git(root, "log", "-1", "--format=%cI")
    if result.returncode != 0:
        raise VerificationError("the consumer checkout has no commit to date the ledgers by")
    return result.stdout[:10]


def require_subject_commit(root: Path, review: Review) -> None:
    label = f"verdict {review.id} subject"
    if _git(root, "cat-file", "-e", f"{review.commit}^{{commit}}").returncode != 0:
        raise VerificationError(
            f"{label}.commit is not present in this checkout; fetch the full history"
        )
    if _git(root, "merge-base", "--is-ancestor", review.commit, "HEAD").returncode != 0:
        raise VerificationError(f"{label}.commit is not an ancestor of the checked-out commit")
    if review.release is None:
        return
    result = _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{review.release}^{{commit}}")
    if result.returncode != 0:
        raise VerificationError(
            f"{label}.release tag {review.release} is not present in this checkout; fetch tags"
        )
    if result.stdout.strip() != review.commit:
        raise VerificationError(f"{label}.release tag {review.release} points at another commit")


def _section(lines: Sequence[str], heading: str) -> list[str]:
    start = lines.index(heading) + 1
    return list(takewhile(lambda line: not line.startswith("## "), lines[start:]))


def _items(lines: Iterable[str], *, label: str) -> list[tuple[str, list[str]]]:
    """Group `- ` list items with their two-space continuation lines (no blank lines)."""
    items: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("- "):
            items.append((line[2:], []))
        elif line.startswith("  ") and items:
            items[-1][1].append(line.strip())
        else:
            raise VerificationError(f"{label} has a line outside any list item: {line}")
    return items


def _fields(continuation: Sequence[str], names: Sequence[str], *, label: str) -> dict[str, str]:
    """Read `Name: text` lines, letting a line without a name continue the last one."""
    fields: dict[str, str] = {}
    current: str | None = None
    for line in continuation:
        name = next((name for name in names if line.startswith(f"{name}: ")), None)
        if name is not None:
            current = name
            fields[name] = line[len(name) + 2:].strip()
        elif current is not None:
            fields[current] = f"{fields[current]} {line}".strip()
        else:
            raise VerificationError(f"{label} has text before its first named line: {line}")
    for name in names:
        if not fields.get(name):
            raise VerificationError(f"{label} needs a non-empty '{name}:' line")
    return fields


def _list_section(
    lines: Sequence[str], heading: str, *, label: str
) -> list[tuple[str, list[str]]]:
    """A list section holds items or the single word `none`."""
    content = [line for line in _section(lines, heading) if line.strip()]
    if content == ["none"]:
        return []
    if not content:
        raise VerificationError(f"{label} {heading} must list items or say none")
    return _items(content, label=f"{label} {heading}")


def _table_rows(lines: Iterable[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        match = _TABLE_ROW.fullmatch(line.strip())
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if all(re.fullmatch(r"-+", cell) for cell in cells):
            continue
        rows.append(cells)
    return rows


def _check_subject(lines: Sequence[str], review: Review, *, label: str) -> None:
    rows = _table_rows(_section(lines, SUBJECT_HEADING))
    if not rows or rows[0] != ["Field", "Value"]:
        raise VerificationError(f"{label} Subject must be a Field | Value table")
    if any(len(row) != 2 for row in rows):
        raise VerificationError(f"{label} Subject rows must have two cells")
    expected = dict(
        zip(
            SUBJECT_ROWS,
            (
                review.repository,
                review.commit,
                review.release or "none",
                ", ".join(review.scope),
                f"{review.name}, {review.credential}",
                review.relationship,
                review.verdict,
                review.confidence,
                review.date,
            ),
        )
    )
    actual = {field: value for field, value in rows[1:]}
    if actual != expected:
        differing = sorted(
            field for field in set(expected) | set(actual) if expected.get(field) != actual.get(field)
        )
        raise VerificationError(f"{label} Subject rows differ from the index: {differing}")


def _check_citations(lines: Sequence[str], *, label: str) -> None:
    rows = _table_rows(_section(lines, CITATION_HEADING))
    if not rows or rows[0] != ["Claimed", "Correct", "Correction"]:
        raise VerificationError(f"{label} citation audit must open with the Claimed table")
    audited = 0
    for row in rows[1:]:
        if len(row) != 3 or not row[0] or row[1] not in {"yes", "no"}:
            raise VerificationError(f"{label} citation row must be claimed | yes or no | correction")
        if row[1] == "no" and not row[2]:
            raise VerificationError(f"{label} citation row marked no needs a correction: {row[0]}")
        audited += 1
    if not audited:
        raise VerificationError(f"{label} citation audit has no rows")


def check_verdict(root: Path, review: Review) -> None:
    label = f"verdict {review.id}"
    raw = require_tracked_regular_file(root, review.path, label=label).read_bytes()
    text = canonical_text(raw, label=label)
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != review.sha256:
        raise VerificationError(f"{label} content does not match its listed sha256")
    lines = text.split("\n")
    if lines[0] != f"# Review verdict {review.id}":
        raise VerificationError(f"{label} must open with '# Review verdict {review.id}'")
    expected = [SUBJECT_HEADING, HEADLINE_HEADING, CITATION_HEADING, FINDINGS_HEADING, QUESTIONS_HEADING]
    if review.verdict == "FIX":
        expected.append(CHANGES_HEADING)
    expected += [METHOD_HEADING, ATTESTATION_HEADING]
    headings = [line for line in lines if line.startswith("## ")]
    if headings != expected:
        raise VerificationError(f"{label} sections must be exactly {expected}")

    _check_subject(lines, review, label=label)

    headline = [line for line in _section(lines, HEADLINE_HEADING) if line.strip()]
    if not headline or re.match(rf"{review.verdict}\b", headline[0]) is None:
        raise VerificationError(f"{label} headline must open with {review.verdict}")
    if review.relationship == "author" and "self-review" not in " ".join(headline):
        raise VerificationError(f"{label} is an author verdict and its headline must say self-review")

    _check_citations(lines, label=label)

    critical = 0
    for head, continuation in _list_section(lines, FINDINGS_HEADING, label=label):
        match = _FINDING.match(head)
        if match is None:
            raise VerificationError(f"{label} finding must open with CRITICAL., WARNING. or NOTE.")
        _fields(continuation, FINDING_FIELDS, label=f"{label} finding '{head}'")
        if match.group(1) == "CRITICAL":
            critical += 1
    if review.verdict == "REJECT" and not critical:
        raise VerificationError(f"{label} is REJECT and needs a CRITICAL finding")
    if review.verdict == "ACCEPT" and critical:
        raise VerificationError(f"{label} is ACCEPT and lists a CRITICAL finding")

    for head, continuation in _list_section(lines, QUESTIONS_HEADING, label=label):
        if not head.endswith("?"):
            raise VerificationError(f"{label} open question must end with a question mark: {head}")
        _fields(continuation, QUESTION_FIELDS, label=f"{label} question '{head}'")

    if review.verdict == "FIX":
        changes = _list_section(lines, CHANGES_HEADING, label=label)
        if not changes:
            raise VerificationError(f"{label} is FIX and must list at least one required change")
        for head, continuation in changes:
            fields = _fields(continuation, CHANGE_FIELDS, label=f"{label} change '{head}'")
            if fields["Re-review"] not in {"yes", "no"}:
                raise VerificationError(f"{label} change '{head}' Re-review must be yes or no")

    if not any(line.strip() for line in _section(lines, METHOD_HEADING)):
        raise VerificationError(f"{label} Method must describe what was read and how")

    attestation = [line.strip() for line in _section(lines, ATTESTATION_HEADING) if line.strip()]
    if ATTESTATION_SENTENCE not in " ".join(attestation):
        raise VerificationError(f"{label} attestation must contain the fixed sentence")
    if f"Name: {review.name}" not in attestation or f"Date: {review.date}" not in attestation:
        raise VerificationError(f"{label} attestation must be signed with the index name and date")


def parse_known_unknowns(text: str) -> dict[str, KnownUnknown]:
    entries: list[tuple[str, str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            match = _KNOWN_UNKNOWN_HEADING.fullmatch(line)
            if match is None:
                raise VerificationError(f"known-unknowns heading must be '## KU-NNN: question?': {line}")
            if any(match.group(1) == entry_id for entry_id, _, _ in entries):
                raise VerificationError(f"duplicate known-unknown id: {match.group(1)}")
            entries.append((match.group(1), match.group(2), []))
        elif entries:
            entry_id, _, items = entries[-1]
            if line.startswith("- "):
                items.append(line[2:])
            elif line.startswith("  ") and items:
                items[-1] += " " + line.strip()
            elif line.strip():
                raise VerificationError(f"{entry_id} has an unexpected line: {line}")

    register: dict[str, KnownUnknown] = {}
    for entry_id, question, items in entries:
        if len(items) != len(KNOWN_UNKNOWN_LINES):
            raise VerificationError(
                f"{entry_id} must have exactly the lines {list(KNOWN_UNKNOWN_LINES)}"
            )
        values: list[str] = []
        for name, item in zip(KNOWN_UNKNOWN_LINES, items):
            prefix = f"{name}: "
            if not item.startswith(prefix) or not item[len(prefix):].strip():
                raise VerificationError(f"{entry_id} line must be '{prefix}<text>': {item}")
            values.append(item[len(prefix):].strip())
        _date(values[0], label=f"{entry_id} Raised")
        resolved: tuple[str, str] | None = None
        if values[4] != "open":
            match = _RESOLVED.fullmatch(values[4])
            if match is None:
                raise VerificationError(
                    f"{entry_id} Status must be 'open' or 'resolved YYYY-MM-DD by <source>'"
                )
            resolved = (_date(match.group(1), label=f"{entry_id} resolution date"), match.group(2))
        register[entry_id] = KnownUnknown(question, tuple(values), resolved)
    return register


def check_known_unknowns(
    register: dict[str, KnownUnknown], *, verdict_ids: frozenset[str], bound: str
) -> None:
    for entry_id, entry in register.items():
        require_not_after(entry.values[0], bound, label=f"{entry_id} Raised")
        if entry.resolved is None:
            continue
        resolved_on, source = entry.resolved
        require_not_after(resolved_on, bound, label=f"{entry_id} resolution date")
        if _ID.fullmatch(source) is not None and source not in verdict_ids:
            raise VerificationError(f"{entry_id} is resolved by an unlisted verdict: {source}")


def _entries_by_id(document: object) -> dict[str, object]:
    """Index a document parse_index has already accepted by review id."""
    entries = _mapping(document, label="review index", keys={"reviews", "schema"})["reviews"]
    assert isinstance(entries, list)
    return {entry["id"]: entry for entry in entries}


def _base_document(root: Path, base: str, path: str) -> bytes | None:
    if _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        raise VerificationError(f"base revision is not a commit in this checkout: {base}")
    result = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        cwd=root,
        check=False,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def require_append_only(
    root: Path,
    base: str,
    current_index: object,
    current_register: dict[str, KnownUnknown],
) -> None:
    raw_index = _base_document(root, base, INDEX_PATH)
    if raw_index is not None:
        base_index = load_index_document(raw_index, label=f"base {base} review index")
        parse_index(base_index)
        listed = _entries_by_id(current_index)
        for review_id, entry in _entries_by_id(base_index).items():
            if listed.get(review_id) != entry:
                raise VerificationError(
                    f"review {review_id} was listed at {base} and must not change or disappear"
                )
    raw_register = _base_document(root, base, KNOWN_UNKNOWNS_PATH)
    if raw_register is None:
        return
    base_register = parse_known_unknowns(canonical_text(raw_register, label=f"base {base} register"))
    for entry_id, before in base_register.items():
        after = current_register.get(entry_id)
        if after is None:
            raise VerificationError(f"{entry_id} was registered at {base} and must not disappear")
        if after.question != before.question or after.values[:4] != before.values[:4]:
            raise VerificationError(f"{entry_id} may change only its Status line after {base}")
        if after.values[4] != before.values[4] and not (before.open and not after.open):
            raise VerificationError(f"{entry_id} Status may only move from open to resolved")


def check_consumer(root: Path, *, base: str | None = None) -> str:
    bound = head_date(root)
    raw = require_tracked_regular_file(root, INDEX_PATH, label="review index").read_bytes()
    document = load_index_document(raw, label="review index")
    reviews = parse_index(document)
    for review in reviews:
        require_not_after(review.date, bound, label=f"review {review.id} date")
        check_verdict(root, review)
        require_subject_commit(root, review)
    label = "known-unknowns register"
    raw = require_tracked_regular_file(root, KNOWN_UNKNOWNS_PATH, label=label).read_bytes()
    register = parse_known_unknowns(canonical_text(raw, label=label))
    check_known_unknowns(
        register, verdict_ids=frozenset(review.id for review in reviews), bound=bound
    )
    if base is not None:
        require_append_only(root, base, document, register)
    open_count = sum(entry.open for entry in register.values())
    return (
        f"reviews valid: {len(reviews)} verdicts, {len(register)} known unknowns "
        f"({open_count} open)"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--base",
        help="a revision whose ledgers must survive unchanged, such as origin/main",
    )
    arguments = parser.parse_args(argv)
    try:
        print(check_consumer(arguments.root, base=arguments.base))
    except VerificationError as error:
        print(f"check_reviews: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
