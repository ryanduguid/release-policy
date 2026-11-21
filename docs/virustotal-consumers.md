## Scanning release assets with VirusTotal

`virustotal-release.yml` uploads every asset of a published release in a public repository to
VirusTotal and adds a VirusTotal section to the release notes, with one link
per asset to `https://www.virustotal.com/gui/file/<sha256>`. It does not wait
for the analysis or block anything on its verdict; the links show the results
once VirusTotal finishes.

The `scan` job holds only `contents: read` and the API key. It downloads the
assets, checks each against the SHA-256 digest GitHub reports for it, and
uploads them one at a time at the public API's rate of four requests a minute.
The `annotate` job holds `contents: write` and no key; it replaces the marked
section of the notes, so a rerun leaves one section. If the release or its
asset list changes during scanning, the job stops before editing the notes.
Immutable releases still
allow their notes to be edited; their assets and tag stay locked.

Add the key as the repository secret `VT_API_KEY`. A release made by a
workflow with `GITHUB_TOKEN` raises no `release` event, so follow the release
workflows instead:

    on:
      workflow_run:
        workflows: [Release]
        types: [completed]
      workflow_dispatch:
        inputs:
          tag:
            description: Published release tag to scan.
            required: true
            type: string

    permissions:
      contents: read

    jobs:
      scan:
        if: >-
          github.event_name == 'workflow_dispatch'
          || (github.event.workflow_run.conclusion == 'success'
              && github.event.workflow_run.event == 'push'
              && github.event.workflow_run.head_repository.full_name == github.repository)
        permissions:
          contents: write # The reusable workflow edits the release notes in a separate job.
        uses: ryanduguid/release-policy/.github/workflows/virustotal-release.yml@<full-40-char-commit-sha>
        with:
          tag: ${{ github.event_name == 'workflow_dispatch' && inputs.tag || github.event.workflow_run.head_branch }}
        secrets:
          vt-api-key: ${{ secrets.VT_API_KEY }}

Replace `<full-40-char-commit-sha>` with a commit you have reviewed. This
prints the commit of this repository's latest release:

    gh api repos/ryanduguid/release-policy/commits/$(gh api repos/ryanduguid/release-policy/releases/latest --jq .tag_name) --jq .sha

List every release workflow's `name` under `workflows`. For releases created
by hand, trigger on `release: types: [published]` and pass
`github.event.release.tag_name` as the tag. Use `workflow_dispatch` to scan a
release published before the caller existed.

Asset names must be plain file names (letters, digits, `.`, `_`, `+` and `-`,
with a first character other than `.`). The workflow checks the size of every asset
before downloading the release. One asset over 650,000,000 bytes stops the entire scan.
Files up to 32 MiB use the direct upload endpoint. Each larger file gets its own
single-use HTTPS upload URL. The lookup and the upload both count towards the
request limit. [VirusTotal large-file uploads](https://docs.virustotal.com/reference/files-upload-url)
