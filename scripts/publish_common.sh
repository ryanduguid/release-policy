#!/usr/bin/env bash
# Shared publication core for publish_python.sh and publish_archives.sh.
# Sourced, never executed on its own.
#
# Both publish paths must survive the same GitHub state, so the draft
# creation, asset inventory, inspection, publication and verification are
# written once here. The families differ only in which assets they ship and
# where those assets sit, which stays in each caller.
#
# The caller sets `tag`, `expected_commit`, `source_path` and the `assets`
# array of file paths, then calls `create_draft`, `upload_asset` once per
# asset, and `finalise_release`.

# Fail at source time, not half way through a publication, when a caller
# forgets one of them. The assignments also tell shellcheck these come from
# the caller.
tag="${tag:?publish_common.sh: the caller must set tag}"
expected_commit="${expected_commit:?publish_common.sh: the caller must set expected_commit}"
source_path="${source_path:?publish_common.sh: the caller must set source_path}"
assets=("${assets[@]:?publish_common.sh: the caller must set the assets array}")

release_id=""
published=false

cleanup_current_draft() {
  status="$?"
  trap - EXIT
  if [ "$status" -ne 0 ] && [ -n "$release_id" ] && [ "$published" != true ]; then
    if gh api \
        -H "X-GitHub-Api-Version: 2026-03-10" \
        "repos/$GITHUB_REPOSITORY/releases/$release_id" \
        > /tmp/cleanup-release.json \
      && jq -e \
        --argjson release_id "$release_id" \
        --arg tag "$tag" \
        '.id == $release_id and .draft == true and .prerelease == false
         and .tag_name == $tag and .name == $tag' \
        /tmp/cleanup-release.json >/dev/null; then
      gh api --method DELETE \
        -H "X-GitHub-Api-Version: 2026-03-10" \
        "repos/$GITHUB_REPOSITORY/releases/$release_id" \
        >/dev/null
    else
      echo "refusing cleanup because the exact current draft identity was not proven" >&2
    fi
  fi
  exit "$status"
}

create_draft() {
  jq -n \
    --arg tag "$tag" \
    --rawfile body "$source_path/RELEASE_NOTES.md" \
    '{tag_name: $tag, name: $tag, body: $body,
      draft: true, prerelease: false}' \
    > /tmp/create-release.json
  # The existing-tag check sits immediately before creating the draft, so a
  # tag that moved between the workflow's own re-check and this point cannot
  # acquire a release.
  create_tag_commit="$(git ls-remote \
    "https://github.com/$GITHUB_REPOSITORY.git" \
    "refs/tags/$tag^{}" | cut -f1)"
  test -n "$create_tag_commit"
  test "$create_tag_commit" = "$expected_commit"
  gh api --method POST \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$GITHUB_REPOSITORY/releases" \
    --input /tmp/create-release.json \
    > /tmp/created-release.json
  release_id="$(jq -er '.id | select(type == "number") | tostring' \
    /tmp/created-release.json)"
  upload_url="https://uploads.github.com/repos/$GITHUB_REPOSITORY/releases/$release_id/assets"
  # A fresh draft can report an untagged-* placeholder until its tag settles.
  # Retry only that state, always by the ID returned from creation.
  for attempt in 1 2 3 4 5; do
    jq -e --argjson release_id "$release_id" --arg tag "$tag" \
      '.id == $release_id and .draft == true and .prerelease == false
       and .name == $tag' /tmp/created-release.json >/dev/null
    if jq -e --arg tag "$tag" '.tag_name == $tag' \
        /tmp/created-release.json >/dev/null; then
      break
    fi
    jq -e '.tag_name | startswith("untagged-")' /tmp/created-release.json >/dev/null
    test "$attempt" -lt 5
    sleep 5
    gh api -H "X-GitHub-Api-Version: 2026-03-10" \
      "repos/$GITHUB_REPOSITORY/releases/$release_id" > /tmp/created-release.json
  done
  # Keep raw jq output byte-exact on Windows; allow only CRLF/LF equivalence in notes.
  jq -bj '.body' /tmp/created-release.json > /tmp/created-release-notes.md
  diff -u --strip-trailing-cr "$source_path/RELEASE_NOTES.md" /tmp/created-release-notes.md
}

# The label is optional: the source-archive family names each asset in the
# release page's list, the Python family ships the wheel and sdist unlabelled.
upload_asset() {
  file="$1"
  media_type="$2"
  label="${3:-}"
  name="$(basename "$file")"
  [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]]
  query="name=$name"
  if [ -n "$label" ]; then
    query="$query&label=$label"
  fi
  gh api --method POST \
    -H "Content-Type: $media_type" \
    "$upload_url?$query" \
    --input "$file" \
    >/dev/null
}

finalise_release() {
  # The asset inventory is derived from the same array the caller uploaded
  # from, so the expected names and digests cannot drift from what was sent.
  expected_assets="$(printf '%s\n' "${assets[@]##*/}" | LC_ALL=C sort)"
  for file in "${assets[@]}"; do
    printf '%s\tsha256:%s\n' \
      "$(basename "$file")" \
      "$(sha256sum "$file" | cut -d' ' -f1)"
  done | LC_ALL=C sort > /tmp/expected-digests

  # GitHub computes asset digests asynchronously, so a freshly uploaded
  # asset can still report a null digest. Retry until every digest is a
  # string and the whole inventory matches.
  draft_ready=false
  for _ in 1 2 3 4 5; do
    gh api \
      -H "X-GitHub-Api-Version: 2026-03-10" \
      "repos/$GITHUB_REPOSITORY/releases/$release_id" \
      > /tmp/draft-release.json
    jq -br '.assets[] | [.name, .digest] | @tsv' \
      /tmp/draft-release.json | LC_ALL=C sort > /tmp/draft-digests
    if jq -e \
        --argjson release_id "$release_id" \
        --argjson count "${#assets[@]}" \
        --arg tag "$tag" \
        '.id == $release_id and .draft == true and .prerelease == false
         and .tag_name == $tag and .name == $tag
         and (.assets | length) == $count
         and all(.assets[]; (.digest | type) == "string")' \
        /tmp/draft-release.json >/dev/null \
      && diff -u /tmp/expected-digests /tmp/draft-digests; then
      draft_ready=true
      break
    fi
    sleep 5
  done
  test "$draft_ready" = true
  draft_assets="$(jq -br '.assets[].name' /tmp/draft-release.json | LC_ALL=C sort)"
  test "$draft_assets" = "$expected_assets"
  jq -bj '.body' /tmp/draft-release.json > /tmp/draft-release-notes.md
  diff -u --strip-trailing-cr "$source_path/RELEASE_NOTES.md" /tmp/draft-release-notes.md

  # Leave the identified draft untouched if the release tag or main moved
  # while the draft was being inspected.
  final_tag_commit="$(git ls-remote \
    "https://github.com/$GITHUB_REPOSITORY.git" \
    "refs/tags/$tag^{}" | cut -f1)"
  test -n "$final_tag_commit"
  test "$final_tag_commit" = "$expected_commit"
  test "$GITHUB_SHA" = "$expected_commit"
  # shellcheck source=scripts/gates.sh
  . "$GITHUB_WORKSPACE/policy/scripts/gates.sh"
  gate_main_matches "$expected_commit" "$GITHUB_REPOSITORY"
  # The draft this run created must be the only release carrying the tag.
  release_ids="$(gh api --paginate \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$GITHUB_REPOSITORY/releases?per_page=100" \
    --jq ".[] | select(.tag_name == \"$tag\") | .id")"
  test "$release_ids" = "$release_id"

  gh api --method PATCH \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$GITHUB_REPOSITORY/releases/$release_id" \
    -F draft=false \
    -f make_latest=true \
    >/dev/null
  published=true

  published_ready=false
  for _ in 1 2 3 4 5; do
    gh api \
      -H "X-GitHub-Api-Version: 2026-03-10" \
      "repos/$GITHUB_REPOSITORY/releases/$release_id" \
      > /tmp/published-release.json
    jq -br '.assets[] | [.name, .digest] | @tsv' \
      /tmp/published-release.json | LC_ALL=C sort > /tmp/published-digests
    if jq -e \
        --argjson release_id "$release_id" \
        --argjson count "${#assets[@]}" \
        --arg tag "$tag" \
        '.id == $release_id and .draft == false and .prerelease == false
         and .immutable == true and .tag_name == $tag and .name == $tag
         and (.assets | length) == $count' \
        /tmp/published-release.json >/dev/null \
      && diff -u /tmp/expected-digests /tmp/published-digests; then
      published_ready=true
      break
    fi
    sleep 5
  done
  test "$published_ready" = true
  test "$(git ls-remote \
    "https://github.com/$GITHUB_REPOSITORY.git" \
    "refs/tags/$tag^{}" | cut -f1)" = "$expected_commit"
  published_assets="$(jq -br '.assets[].name' \
    /tmp/published-release.json | LC_ALL=C sort)"
  test "$published_assets" = "$expected_assets"
  jq -bj '.body' /tmp/published-release.json > /tmp/published-release-notes.md
  diff -u --strip-trailing-cr "$source_path/RELEASE_NOTES.md" /tmp/published-release-notes.md

  # Another component can become latest while this release is being verified.
  # Release identity, immutability and asset checks above remain authoritative.

  release_verified=false
  for _ in 1 2 3 4 5; do
    if gh release verify "$tag" --repo "$GITHUB_REPOSITORY"; then
      release_verified=true
      break
    fi
    sleep 5
  done
  test "$release_verified" = true
  for file in "${assets[@]}"; do
    asset_verified=false
    for _ in 1 2 3 4 5; do
      if gh release verify-asset "$tag" "$file" --repo "$GITHUB_REPOSITORY"; then
        asset_verified=true
        break
      fi
      sleep 5
    done
    test "$asset_verified" = true
  done
  trap - EXIT
}
