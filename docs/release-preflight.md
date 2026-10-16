# Verify the release candidate before tagging

Before asking for annotated-tag approval, freeze the candidate main SHA and read
the release caller and component configuration from that commit. Record the
caller path, adapter, published full-SHA policy pin and mandatory selectors.
Confirm that the candidate is still the public remote main tip.

Check the calling job's effective `actions: read` permission and the publication
permissions required by its adapter. Use the existing component validators for
source paths, tag namespace, artifact identifiers, version and release notes.
Check the selected component's lock and build prerequisites against
[consumer prerequisites](consumer-prerequisites.md).

Extract the reviewed caller's `required-checks` input to a temporary text file.
Keep that extraction with the candidate record; do not maintain a second list of
selectors. Run the verifier from the reviewed, published policy revision:

```sh
python /path/to/reviewed-policy/scripts/required_checks.py \
  --repository "$REPOSITORY" \
  --commit "$CANDIDATE_SHA" \
  --wait-seconds 600 < "$REVIEWED_SELECTORS_FILE"
```

This read-only command rejects empty, malformed and duplicate selectors and
requires exactly one successful selected job in every trusted main execution
for that SHA. It catches ambiguous display names even when both jobs passed.
Missing runs fail immediately; the bounded wait applies to reported pending
checks. A failure stops tag preparation.

Reconfirm the public remote main tip and the reviewed configuration immediately
before requesting tag approval. A changed candidate invalidates the record.
The human-created annotated tag remains the approval act. Preflight documents
that procedure; it does not technically prevent someone bypassing it with a
direct tag command. Keep the post-tag verification because the tag does not yet
exist during preflight.

The gate-test runner prints elapsed seconds for each case, including the two
publication fixture families. Compare equivalent runners and inputs before
changing those fixtures or claiming savings.
