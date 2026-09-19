"""Check a consumer's review index, verdict files and known-unknowns register.

The rules are those in docs/review-verdicts.md. The check reads the consumer
checkout and its Git index only; it reaches no network and writes nothing. It
stops at the first failure, like verify_skills.py, so a maintainer fixes one
named defect at a time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
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
HEADINGS_BEFORE_FIX = (
    "## Subject",
    "## 1. Headline verdict",
    "## 2. Citation audit",
    "## 3. Findings",
    "## 4. Open questions",
)
HEADING_FIX = "## 5. Required changes"
HEADINGS_AFTER_FIX = ("## 6. Method", "## 7. Attestation")
KNOWN_UNKNOWN_LINES = ("Raised", "Affects", "Why it matters", "Resolution path", "Status")

_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_PROVISION_SECTION = r"[A-Za-z0-9]+(?:\([A-Za-z0-9]+\)(?:-[A-Za-z0-9]+(?:\([A-Za-z0-9]+\))?)*)*"
_NON_STATUTORY = r"(?:APES_[0-9]+|TPB_GS_[0-9]+)"

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_RELEASE = re.compile(
    r"(?:[a-z0-9]+(?:-[a-z0-9]+)*/)?"
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_PROVISION = re.compile(
    rf"(?:[A-Z][A-Z0-9]*_[0-9]{{4}}_(?:s|Div|Subdiv|Pt)_{_PROVISION_SECTION}|{_NON_STATUTORY})\Z"
)
_FINDING = re.compile(r"- (CRITICAL|WARNING|NOTE)\. \S")
_KNOWN_UNKNOWN_HEADING = re.compile(r"## (KU-[0-9]{3}): (.+\?)\Z")
_RESOLVED = re.compile(r"resolved ([0-9]{4}-[0-9]{2}-[0-9]{2}) by (\S.*)\Z")


@dataclass(frozen=True)
class Review:
    id: str
    path: str
    sha256: str
    commit: str
    verdict: str
    relationship: str
    release: str | None
    review_date: str


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
        _date(review_id[:10], label=f"{label}.id date")
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
        _pattern(
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
        if subject["release"] is not None:
            _pattern(
                subject["release"],
                label=f"{label} subject.release",
                pattern=_RELEASE,
                form="a release tag such as v1.2.3 or prefix/v1.2.3",
            )
        scope = entry["scope"]
        if not isinstance(scope, list) or not scope:
            raise VerificationError(f"{label} scope must be a non-empty list")
        provisions = [
            _pattern(
                item,
                label=f"{label} scope[{index}]",
                pattern=_PROVISION,
                form="ACT_YEAR_unit_SECTION, unit one of s, Div, Subdiv, Pt",
            )
            for index, item in enumerate(scope)
        ]
        if len(set(provisions)) != len(provisions):
            raise VerificationError(f"{label} scope repeats a provision")
        reviewer = _mapping(
            entry["reviewer"],
            label=f"{label} reviewer",
            keys={"credential", "name", "relationship"},
        )
        _text(reviewer["name"], label=f"{label} reviewer.name")
        _text(reviewer["credential"], label=f"{label} reviewer.credential")
        relationship = _choice(
            reviewer["relationship"], label=f"{label} reviewer.relationship", allowed=RELATIONSHIPS
        )
        verdict = _choice(entry["verdict"], label=f"{label} verdict", allowed=VERDICTS)
        _choice(entry["confidence"], label=f"{label} confidence", allowed=CONFIDENCES)
        review_date = _date(entry["date"], label=f"{label} date")
        supersedes = entry["supersedes"]
        if supersedes is not None and supersedes not in seen:
            raise VerificationError(f"{label} supersedes must name an earlier listed review")
        seen.append(review_id)
        reviews.append(Review(review_id, path, digest, commit, verdict, relationship, subject["release"], review_date))

    if seen != sorted(seen):
        raise VerificationError("review index must be sorted by id")
    return tuple(reviews)


def load_index(root: Path) -> tuple[Review, ...]:
    raw = require_tracked_regular_file(root, INDEX_PATH, label="review index").read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise VerificationError("review index must not contain a UTF-8 BOM")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError("review index must be valid UTF-8 JSON") from error
    if raw != (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"):
        raise VerificationError("review index must use canonical sorted JSON")
    return parse_index(document)


def _section(lines: Sequence[str], heading: str) -> list[str]:
    start = lines.index(heading) + 1
    return list(takewhile(lambda line: not line.startswith("## "), lines[start:]))


def check_verdict(root: Path, review: Review) -> None:
    label = f"verdict {review.id}"
    raw = require_tracked_regular_file(root, review.path, label=label).read_bytes()
    text = canonical_text(raw, label=label)
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != review.sha256:
        raise VerificationError(f"{label} content does not match its listed sha256")
    lines = text.split("\n")
    if lines[0] != f"# Review verdict {review.id}":
        raise VerificationError(f"{label} must open with '# Review verdict {review.id}'")
    expected = list(HEADINGS_BEFORE_FIX)
    if review.verdict == "FIX":
        expected.append(HEADING_FIX)
    expected.extend(HEADINGS_AFTER_FIX)
    headings = [line for line in lines if line.startswith("## ")]
    if headings != expected:
        raise VerificationError(f"{label} sections must be exactly {expected}")
    subject = _section(lines, "## Subject")
    subject_text = "\n".join(subject)
    relationship_words = re.findall(r"relationship\s*\|\s*(author|independent)", subject_text, re.I)
    if len(relationship_words) != 1 or relationship_words[0].lower() != review.relationship:
        raise VerificationError(f"{label} Subject must disclose reviewer relationship")
    if review.relationship == "author" and ("self-review" not in subject_text.lower() or "independent" in subject_text.lower()):
        raise VerificationError(f"{label} author review must explicitly disclose self-review")
    citation = _section(lines, "## 2. Citation audit")
    if not any(line.startswith("| Claimed | Correct | Correction |") for line in citation) or not any(line.startswith("| ") and "| yes |" in line or line.startswith("| ") and "| no |" in line for line in citation):
        raise VerificationError(f"{label} citation audit must contain its table and rows")
    method = [line for line in _section(lines, "## 6. Method") if line.strip()]
    attestation = [line for line in _section(lines, "## 7. Attestation") if line.strip()]
    if not method or not any(line.startswith("Name: ") for line in attestation) or not any(line.startswith("Date: ") for line in attestation):
        raise VerificationError(f"{label} method and attestation are incomplete")
    headline = [line for line in _section(lines, "## 1. Headline verdict") if line.strip()]
    if not headline or re.match(rf"{review.verdict}\b", headline[0]) is None:
        raise VerificationError(f"{label} headline must open with {review.verdict}")
    critical = 0
    for item in _section(lines, "## 3. Findings"):
        if not item.startswith("- "):
            continue
        match = _FINDING.match(item)
        if match is None:
            raise VerificationError(f"{label} finding must open with CRITICAL., WARNING. or NOTE.")
        if match.group(1) == "CRITICAL":
            critical += 1
    if review.verdict == "REJECT" and not critical:
        raise VerificationError(f"{label} is REJECT and needs a CRITICAL finding")
    if review.verdict == "ACCEPT" and critical:
        raise VerificationError(f"{label} is ACCEPT and lists a CRITICAL finding")


def require_ancestor(root: Path, review: Review) -> None:
    commit_date = subprocess.run(["git", "show", "-s", "--format=%cI", review.commit], cwd=root, check=False, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if commit_date.returncode != 0:
        raise VerificationError(f"verdict {review.id} subject.commit is not available locally; fetch full history")
    try:
        cutoff = datetime.fromisoformat(commit_date.stdout.strip()).date()
    except ValueError as error:
        raise VerificationError(f"verdict {review.id} subject.commit has no valid commit date") from error
    if date.fromisoformat(review.review_date) > cutoff:
        raise VerificationError(f"verdict {review.id} date is later than subject.commit date")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", review.commit, "HEAD"],
        cwd=root,
        check=False,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"verdict {review.id} subject.commit is not an ancestor of the checked-out commit"
        )


def parse_known_unknowns(text: str, *, verdict_ids: frozenset[str]) -> tuple[int, int]:
    """Return the entry count and the open count, refusing a malformed register."""
    entries: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            match = _KNOWN_UNKNOWN_HEADING.fullmatch(line)
            if match is None:
                raise VerificationError(f"known-unknowns heading must be '## KU-NNN: question?': {line}")
            if any(match.group(1) == entry_id for entry_id, _ in entries):
                raise VerificationError(f"duplicate known-unknown id: {match.group(1)}")
            entries.append((match.group(1), []))
        elif entries:
            entry_id, items = entries[-1]
            if line.startswith("- "):
                items.append(line[2:])
            elif line.startswith("  ") and items:
                items[-1] += " " + line.strip()
            elif line.strip():
                raise VerificationError(f"{entry_id} has an unexpected line: {line}")

    open_count = 0
    for entry_id, items in entries:
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
        status = values[4]
        if status == "open":
            open_count += 1
            continue
        resolved = _RESOLVED.fullmatch(status)
        if resolved is None:
            raise VerificationError(
                f"{entry_id} Status must be 'open' or 'resolved YYYY-MM-DD by <source>'"
            )
        _date(resolved.group(1), label=f"{entry_id} resolution date")
        source = resolved.group(2)
        if _ID.fullmatch(source) is not None and source not in verdict_ids:
            raise VerificationError(f"{entry_id} is resolved by an unlisted verdict: {source}")
    return len(entries), open_count


def check_consumer(root: Path) -> str:
    reviews = load_index(root)
    for review in reviews:
        check_verdict(root, review)
        require_ancestor(root, review)
    label = "known-unknowns register"
    raw = require_tracked_regular_file(root, KNOWN_UNKNOWNS_PATH, label=label).read_bytes()
    total, open_count = parse_known_unknowns(
        canonical_text(raw, label=label),
        verdict_ids=frozenset(review.id for review in reviews),
    )
    return f"reviews valid: {len(reviews)} verdicts, {total} known unknowns ({open_count} open)"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    try:
        print(check_consumer(arguments.root))
    except VerificationError as error:
        print(f"check_reviews: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
