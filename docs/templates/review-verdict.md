# Review verdict YYYY-MM-DD-short-title

## Subject

| Field | Value |
| --- | --- |
| Repository | owner/name |
| Commit | full 40-character SHA |
| Release | vX.Y.Z or none |
| Scope | the index scope list, joined by comma and space |
| Reviewer | Name, credential |
| Relationship | author or independent |
| Verdict | ACCEPT, REJECT or FIX |
| Confidence | HIGH, MEDIUM or LOW |
| Date | YYYY-MM-DD |

## 1. Headline verdict

VERDICT. One paragraph. State what the subject encodes correctly for the scope
above and, for REJECT or FIX, what is wrong in plain words. An author verdict
says here that it is a self-review. No hedging here; the hedges belong in
sections 3 and 4.

## 2. Citation audit

| Claimed | Correct | Correction |
| --- | --- | --- |
| ITAA 1997 s 000-00 | yes | |
| ITAA 1997 s 000-00(2) | no | The rule is in s 000-00(3); subsection (2) defines the term. |

Provisions the subject should cite and does not:

- none

## 3. Findings

- CRITICAL. The finding in one sentence.
  Why it matters: one sentence naming the wrong output and the input that
  produces it.
  Remedy: add validation, refuse with an error, disclaim in the
  documentation, or a separate calculator.
- WARNING. The finding in one sentence.
  Why it matters: one sentence.
  Remedy: one sentence.
- NOTE. The finding in one sentence.
  Why it matters: one sentence.
  Remedy: one sentence.

## 4. Open questions

- The question, as a sentence ending in a question mark.
  Why it matters: one sentence.
  Resolution path: the source, ruling or person that would settle it.

## 5. Required changes

- Defect: plain words, FIX only; delete the whole section for ACCEPT and REJECT.
  Change: plain words.
  Re-review: yes or no

## 6. Method

Materials read, time spent in hours, and every tool used to search or draft.
A tool may draft; the reviewer reads and signs.

## 7. Attestation

I read the subject at the commit above and the materials named in section 6.
The verdict is my own professional judgement. I understand this file will be
digested and listed in the repository's review index and that it will not be
edited after listing.

Name: Reviewer Name
Date: YYYY-MM-DD
