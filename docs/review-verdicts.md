## Review verdicts and known unknowns

Status: draft. `scripts/check_reviews.py` enforces the shapes below, but no
reusable workflow calls it yet, so no consumer pin changes because this
document exists. Binding an accepted verdict to a release is a later reviewed
change.

A review verdict records that a named person read a calculator or guide at one
commit and judged whether it encodes the law or standard it claims to encode.
A known-unknowns file records the questions the maintainer could not settle
from primary sources. Both are release evidence of the same kind as the canary
manifest: they say who checked what, at which commit, and what they could not
check. Neither is proof of accounting correctness.

## Files in a consumer

| Path | Owner | Mutability |
| --- | --- | --- |
| `reviews/index.json` | maintainer | append-only |
| `reviews/<id>.md` | reviewer | immutable once listed |
| `docs/known-unknowns.md` | maintainer | entries close, never disappear |

Templates: [review-verdict.md](templates/review-verdict.md) and
[known-unknowns.md](templates/known-unknowns.md).

## `reviews/index.json`

One JSON document in the canonical form `canaries.json` uses (two-space
indent, keys sorted, trailing newline), `schema` 1, `reviews` sorted by `id`:

    {
      "reviews": [
        {
          "confidence": "MEDIUM",
          "date": "2026-11-16",
          "id": "2026-11-16-sources-review",
          "path": "reviews/2026-11-16-sources-review.md",
          "reviewer": {
            "credential": "Registered Tax Agent",
            "name": "Example Reviewer",
            "relationship": "independent"
          },
          "scope": [
            "ITAA_1936_s_109E",
            "TAA_1953_s_284-75"
          ],
          "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
          "subject": {
            "commit": "1111111111111111111111111111111111111111",
            "release": "v0.3.3",
            "repository": "ryanduguid/example-calculator"
          },
          "supersedes": null,
          "verdict": "FIX"
        }
      ],
      "schema": 1
    }

Field rules, all of which fail closed in the checker:

- `id`: `YYYY-MM-DD-` followed by lower-case hyphenated words, unique in the
  index, never reused. It is also the verdict file's stem and title.
- `path`: `reviews/<id>.md`, a tracked regular file with the same safe
  relative path rules the skill verifier applies.
- `sha256`: hex digest of the verdict file after CRLF is normalised to LF,
  the same canonical form `tests/test_repository_baseline.sh` uses.
- `subject.commit`: full 40-character commit of the reviewed repository, an
  ancestor of the commit the checker runs against. `subject.release` is the
  tag that commit carries, `v1.2.3` or `prefix/v1.2.3`, or `null` when the
  review is of an unreleased commit. `subject.repository` is `owner/name`.
- `scope`: non-empty list of distinct provision identifiers,
  `ACT_YEAR_unit_SECTION`, the unit one of `s`, `Div`, `Subdiv` or `Pt` and
  the section as written in the Act (`ITAA_1936_s_109E`, `TAA_1953_s_284-75`,
  `ITAA_1997_Subdiv_328-D`). The identifier names what was reviewed, not what
  the subject cites.
- `reviewer.relationship`: `author` when the reviewer maintains the subject,
  otherwise `independent`. An `author` verdict is a self-review and every
  rendering of it must say so; it never counts as independent attestation.
- `reviewer.name`: a person. Tool assistance is disclosed in the verdict's
  Method section, and a tool is never the reviewer.
- `verdict`: `ACCEPT`, `REJECT` or `FIX`. `confidence`: `HIGH`, `MEDIUM` or
  `LOW`. `date`: the day the reviewer signed, ISO 8601, not later than the
  index commit date.
- `supersedes`: an earlier `id` or `null`. A correction is a new verdict that
  supersedes the old one. No listed entry or verdict file is ever edited or
  removed; the digest would no longer match and the ledger would stop being
  a ledger. Attribution changes, such as a reviewer opting down to initials,
  are also a superseding entry, which keeps the hashed content intact.

## `reviews/<id>.md`

Markdown, LF line endings, UTF-8 without a byte-order mark. The title line is
`# Review verdict <id>` and the sections below appear in this order with
these headings. A missing or renamed heading fails the checker.

1. `## Subject`: repository, commit, release, scope and reviewer as in the
   index, as a table for readers. The index is authoritative; the pull
   request review checks the two agree.
2. `## 1. Headline verdict`: one paragraph, the verdict word first.
   `ACCEPT` means the encoding matches the law for the stated scope. `REJECT`
   means the encoding is wrong in a way that changes outputs. `FIX` means the
   encoding is broadly right and the listed changes are required before the
   next release.
3. `## 2. Citation audit`: a table with columns `Claimed`, `Correct`, and
   `Correction`. One row for every provision the subject cites within scope.
   `Correct` is `yes` or `no`; a `no` row names the right provision. Rows for
   provisions the subject should cite and does not follow the table.
4. `## 3. Findings`: a list. Each item opens with `CRITICAL`, `WARNING` or
   `NOTE`, then the finding, then `Why it matters:` and `Remedy:` lines.
   `CRITICAL` means a materially wrong output for a lawful input. `WARNING`
   means a defensible output that is not the only defensible one and is not
   disclosed as such. `NOTE` changes no output. A remedy is one of: add
   validation, refuse with an error, disclaim in the documentation, or a
   separate calculator. A `REJECT` verdict has at least one `CRITICAL`
   finding; an `ACCEPT` verdict has none.
5. `## 4. Open questions`: questions the reviewer could not settle from the
   law and public guidance. Each carries `Why it matters:` and `Resolution
   path:` lines. The maintainer copies each one into `docs/known-unknowns.md`
   in the same pull request that lists the verdict.
6. `## 5. Required changes`: present only for `FIX`. Each item names the
   defect, the change, and whether the reviewer wants to re-review the fix.
   A change marked for re-review is not complete until a superseding verdict
   accepts it.
7. `## 6. Method`: what the reviewer read, time spent, and any tool used to
   draft or search. A verdict drafted by a tool and signed unread is not a
   verdict, which is why this section exists.
8. `## 7. Attestation`: the reviewer's name and the date, plus the sentence
   in the template. A typed name is sufficient; the digest binds the file.

## `docs/known-unknowns.md`

Markdown, one heading per entry, ids `KU-` plus a three-digit number that is
never reused. Each entry has the lines `Raised`, `Affects`, `Why it matters`,
`Resolution path` and `Status`, in that order. `Status` is `open`, or
`resolved <date> by <verdict id or citation>`. A resolved entry keeps its
text; only its `Status` line changes.

What belongs here: a question about the law, a rate, a date or a data shape
that the maintainer could not settle from a primary source and that a user of
the subject would want to know. What does not belong here: a defect. A
`CRITICAL` or `WARNING` finding is fixed or disclosed in the output, not
parked as a question. A known unknown that changes an output value must also
be named in the README or in the output itself; the file is the register,
not the disclosure.

## What the checker enforces

`scripts/check_reviews.py` runs against a consumer checkout with
`contents: read`, the same way `verify_skills.py` does. It imports that
verifier's tracked-file helpers, so run it from a checkout of this repository
and point it at the consumer:

    python scripts/check_reviews.py --root /path/to/consumer

It reads no network and writes nothing, and it stops at the first failure.
It refuses on any of:

- an index that is not the exact shape above, not canonical JSON, not sorted
  by `id`, or not a tracked regular file;
- a duplicate or malformed `id`, a `path` other than `reviews/<id>.md`, a
  `path` that is not a tracked regular file, or a digest mismatch;
- a `subject.commit` that is not an ancestor of the checked-out commit, or a
  `supersedes` that names no earlier entry;
- a verdict file whose title is wrong or whose `## ` headings are not exactly
  the required set in order, including section 5 present without `FIX` or
  absent with it;
- a headline that does not open with the listed verdict word, a finding item
  that does not open with `CRITICAL.`, `WARNING.` or `NOTE.`, a `REJECT`
  without a `CRITICAL` finding, or an `ACCEPT` with one;
- a known-unknowns file that is not a tracked regular file, a heading that is
  not `## KU-NNN: question?`, a reused id, an entry whose lines are not
  exactly the five named ones in order, a malformed date or status, or a
  status resolved by a verdict id the index does not list.

Binding a verdict to a release, so that a tag on a commit with an open
`REJECT` or an unaccepted `FIX` cannot publish, is a later reviewed change to
the release adapters and needs its own design note.
