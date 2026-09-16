# The egress check

`scripts/check_egress.py` reads a publication candidate and refuses it if it
carries content that should not leave private hands. It is a standalone
script: no reusable workflow calls it, and no consumer pin changes because it
exists. Adopt it by adding a step, not by moving a pin.

## Why it runs on egress and not on commit

A hook on the way into a private repository blocks authoring the material this
check exists to contain. Private notes, client identifiers and internal
hostnames all have to be written somewhere. The rule is that they are written
freely and checked once, on the way out: the same shape as a one-way
firewall, where content flows private to public through a deterministic check
and nothing flows back.

That is why this check is deliberately absent from `.pre-commit-config.yaml`.

## The three layers

| Layer | What it looks for | On a finding |
| --- | --- | --- |
| A | terms from the operator's private terms file | hard fail |
| B | structural shapes: user paths, every RFC 1918 range, loopback, internal hostnames, bucket URIs, dotenv references, UNC shares | soft fail; `--allow-structural` waves them through |
| C | the reserved namespace `x-internal-` | hard fail |

A layer A finding reports the candidate file and line, and the line in the
terms file that matched. It never prints the term or the text around it. Both
are the private material this check exists to contain, and a CI log is read by
more people than the artefact would have reached. The operator has the terms
file; the line number is enough to act on.

Layer A's terms live in a file that is never committed. A public repository
carrying the list of words it must not publish has already published them.
Keep the file outside the checkout, or somewhere `.gitignore` covers, and pass
it with `--terms`.

Layer C works the other way round: anything internal is marked with the
`x-internal-` prefix where it is written, so the check needs no knowledge of
what the value means to refuse it.

## Exit status

| Status | Meaning |
| --- | --- |
| `0` | nothing to report |
| `1` | a finding: do not publish |
| `2` | the check did not run, so nothing was checked |

The 2 band is what stops a broken gate reading as a clean one. A missing terms
file, an unreadable candidate or a pattern that does not compile all land
there and print `nothing was checked; this candidate is unchecked, not clean`.

The check also fails closed on its own configuration: run with neither
`--terms` nor `--no-terms` and it refuses to report a verdict at all, because
a clean result that silently skipped layer A is worse than no result. A terms
file that exists but lists no terms is an error for the same reason: an
emptied policy file is the likeliest way this gate would go quiet.

## Excluding a file that has to carry the shapes

This page names the reserved namespace, and the script's own source holds
every structural pattern. Both would fail the check they describe, so
`--exclude GLOB` skips a named file. Two rules keep an exclusion from becoming
a hole: every excluded file is printed as `excluded by --exclude, not
checked`, and a pattern that matches no file in the candidate is an error in
the 2 band, because a stale exclusion protects nothing and hides the next file
that needs looking at.

The pattern is matched against the whole path as given, not the file name, so
`--exclude docs/egress-check.md` excludes that one file and leaves
`docs/nested/egress-check.md` to be scanned. Exclude a file because it must
contain the shapes, never because it is inconvenient that it does.

## Files that cannot be read as text

A file whose suffix is not a known text type, or that does not decode as
UTF-8, is listed as `not scanned as text` and the candidate is **not** cleared.
A gate cannot vouch for bytes it never decoded, and reporting `clean` would be
a claim about exactly those bytes. Pass `--allow-unscanned` once a person has
decided the listed files are fit to publish; they are printed either way.

## Use

```bash
python scripts/check_egress.py dist/ --terms ../private/egress-terms.txt
```

```bash
python scripts/check_egress.py README.md docs --no-terms --allow-structural
```

```bash
python scripts/check_egress.py dist/ --terms ../private/egress-terms.txt --allow-unscanned
```

## What holds it in place

- the verifier, `scripts/check_egress.py`, with the three-state status above
- a CI step in `.github/workflows/ci.yml` over this repository's own published
  prose, layers B and C only, because the terms file is private, with this
  page excluded and a test that fails if that exclusion stops being needed
- `tests/test_egress.py`: a fixture for each layer, a near-miss negative for
  each one so the check does not train its reader to ignore it, and a fixture
  for each exit band including the ones that must report 2 rather than 1
- no pre-commit hook, for the reason at the top of this page
