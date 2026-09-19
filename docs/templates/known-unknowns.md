# Known unknowns

Questions the maintainer could not settle from a primary source. Each entry
stays here after it is resolved; only its status line changes. Ids are never
reused. A defect is not a known unknown: a wrong output is fixed or refused,
not listed. An entry that changes an output value is also disclosed in the
README or in the output itself.

Entry shape, in this order: `Raised`, `Affects`, `Why it matters`,
`Resolution path`, `Status`. Status is `open`, or
`resolved <date> by <verdict id or citation>`.

## KU-001: Does the vendor export label the receipt date, or the date sent?

- Raised: 2026-09-20
- Affects: the due-date comparison in `checker/deadlines.py`
- Why it matters: a payment recorded on the date sent can read as on time
  when the fund received it after the deadline.
- Resolution path: a header row from a real export, or the vendor's published
  column list; neither was available at the raised date.
- Status: open

## KU-002: Which May publication fixes the benchmark rate for a year that starts on a weekend?

- Raised: 2026-09-20
- Affects: the rate table in `rates.json`
- Why it matters: the wrong month's figure changes every repayment amount
  for that year.
- Resolution path: the statutory definition names the last figure published
  before the year starts; confirm the publication date against the source
  series for the year in question.
- Status: resolved 2026-09-20 by 2026-09-20-example-review
