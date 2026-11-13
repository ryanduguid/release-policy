# ADR-0002: ship a skill pack's per-skill ZIPs in one bundle asset

- Status: Accepted
- Date: 2026-11-14
- Deciders: Ryan Duguid

## Context

`skill-archives` ships each listed skill directory as its own upload ZIP and
release asset. A pack with many skills makes that unworkable twice over. Its
caller must list every directory, and every ZIP adds an asset for
`virustotal-release.yml` to upload. A pack of 107 skills would release 112
assets. The scan uploads one asset every 16 seconds, inside the four requests
a minute the Public API allows, so 112 uploads take about 30 minutes: the
whole of the scan job's limit, before the download.

## Decision

A `skill-archives` list may be one line, `<parent>/*`, standing for every
directory directly inside that tracked parent at the tagged commit. The
release builds each directory's ZIP exactly as a per-directory line would,
then stores those ZIPs in one asset, `<stem>-<version>-skills.bundle.zip`:
in byte order of their names, uncompressed, each with the commit's time and
the same mode, so the bundle is deterministic. The release gains one asset,
and the existing scan, checksums and provenance attestation cover it as they
cover a per-directory ZIP. Like those ZIPs, the bundle is not a subject of
the SBOM attestation.

A parent line must be the only line. Mixing it with per-directory lines
would need rules for which ZIPs ship alone and which in the bundle, and for
name clashes between the two, with no caller that needs it. Every directory
the line finds must pass the per-directory checks; one that is not a skill
fails the release rather than being skipped, so a release never publishes a
set no one listed. Per-directory lists behave as before.

## Alternatives considered

- **Scan every ZIP, with a longer scan job.** Rejected by Ryan on 14 November
  2026 in favour of one bundle: he chose not to send about 112 assets a
  release to the Public API.
- **Batch the uploads across a matrix of 30-minute jobs.** Rejected. The rate
  limit belongs to the key, not the job, so batching adds jobs and outputs
  without adding throughput.
- **Scan only the source archive, which holds every skill.** Rejected. It
  leaves the shipped upload ZIPs without any asset of their own in the
  release.
- **A separate `skill-archive-parents` input.** Rejected. It is a second
  input to thread through both workflows for what one line expresses.

## Consequences

- VirusTotal reports on the bundle's digest. The ZIPs inside it get no report
  link of their own; a downloader checks the bundle and then extracts it.
- Uploading a skill to Claude or ChatGPT takes one extra step: extract its ZIP
  from the bundle.
- The bundle is built by `scripts/build_skill_bundle.py`, which has no
  `release-policy` command yet; it is ported with the other workflow scripts
  at the switch-over ([rust-cli.md](../rust-cli.md)).
