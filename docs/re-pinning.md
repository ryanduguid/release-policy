# Re-pinning consumers

Consumers pin each reusable workflow here by commit, so a change reaches a consumer
only when its pin moves, and every moved pin costs that repository a pull request and a
CI run. Move only the pins whose family changed:

```bash
python scripts/pin_families.py <old-commit> <new-commit>
```

It prints `keep the pin` or the changed files for each of 5 families: `release-python`,
`release-archive`, `release-skills`, `verify-skills` and `attribution`. A family is the reusable workflow a
consumer pins and every file it executes, followed from the workflow through the
workflows it calls, the composite action it uses and the scripts it names, sources or
runs. Comment lines are not followed; a script named in other prose is, which can name a
family that did not need a re-pin but never misses one that did. Re-pin the consumers
of each named family and no others. A change outside these families, such as documentation or
tests, moves no pin.

The skill verifier has its own consumer pin. Changes to its workflow or script move both
`verify-skills` and `release-skills`; changes confined to release dependencies leave the
verifier pin unchanged.

The 20 September 2026 round re-pinned all 30 attribution consumers and every release
caller together, although only the attribution family had changed since the previous
release pin.

## Contribution forks

A contribution fork's `No AI attribution` workflow omits the push trigger, as
[Attribution checks](attribution-consumers.md) describes, so its guard sees only pull
requests opened inside the fork. Those forks stay on the attribution pin they carry
(`87767ec` on 27 September 2026) and are left out of routine re-pins. Move a fork's pin
only when a change alters what the check does to a pull request opened inside the fork.
