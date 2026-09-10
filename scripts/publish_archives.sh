#!/usr/bin/env bash
# Publish one already-gated source-archive release without executing consumer code.
set -euo pipefail

if [ "$#" -ne 5 ]; then
  echo "usage: publish_archives.sh TAG EXPECTED_COMMIT STEM VERSION SOURCE_PATH" >&2
  exit 64
fi

tag="$1"
expected_commit="$2"
base="$3-$4"
source_path="$5"
dist="$source_path/dist"
zip="$dist/$base.zip"
tar="$dist/$base.tar.gz"
sbom="$dist/$base.spdx.json"
assets=("$zip" "$tar" "$sbom" "$dist/SHA256SUMS")
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
trap cleanup_current_draft EXIT

jq -n \
  --arg tag "$tag" \
  --rawfile body "$source_path/RELEASE_NOTES.md" \
  '{tag_name: $tag, name: $tag, body: $body,
    draft: true, prerelease: false}' \
  > /tmp/create-release.json
# Preserve the existing-tag check immediately before creating the draft.
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
jq -j '.body' /tmp/created-release.json > /tmp/created-release-notes.md
diff -u "$source_path/RELEASE_NOTES.md" /tmp/created-release-notes.md

upload_asset() {
  file="$1"
  media_type="$2"
  label="$3"
  name="$(basename "$file")"
  [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]]
  gh api --method POST \
    -H "Content-Type: $media_type" \
    "$upload_url?name=$name&label=$label" \
    --input "$file" \
    >/dev/null
}
upload_asset "$zip" application/zip Deterministic%20source%20ZIP
upload_asset "$tar" application/gzip Deterministic%20source%20tar%20archive
upload_asset "$sbom" application/spdx+json SPDX%202.3%20SBOM
upload_asset "$dist/SHA256SUMS" text/plain SHA-256%20checksums

expected_assets="$(printf '%s\n' \
  "SHA256SUMS" \
  "$base.spdx.json" \
  "$base.tar.gz" \
  "$base.zip" | LC_ALL=C sort)"
(
  cd "$dist"
  for file in "SHA256SUMS" "$base.spdx.json" "$base.tar.gz" "$base.zip"; do
    printf '%s\tsha256:%s\n' "$file" "$(sha256sum "$file" | cut -d' ' -f1)"
  done
) | LC_ALL=C sort > /tmp/expected-digests

# GitHub computes asset digests asynchronously, so a freshly uploaded
# asset can still report a null digest. Retry until every digest is a
# string and the whole inventory matches.
draft_ready=false
for _ in 1 2 3 4 5; do
  gh api \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$GITHUB_REPOSITORY/releases/$release_id" \
    > /tmp/draft-release.json
  jq -r '.assets[] | [.name, .digest] | @tsv' \
    /tmp/draft-release.json | LC_ALL=C sort > /tmp/draft-digests
  if jq -e --arg tag "$tag" --argjson release_id "$release_id" \
      '.id == $release_id and .draft == true and .prerelease == false
       and .tag_name == $tag and .name == $tag
       and (.assets | length) == 4
       and all(.assets[]; (.digest | type) == "string")' \
      /tmp/draft-release.json >/dev/null \
    && diff -u /tmp/expected-digests /tmp/draft-digests; then
    draft_ready=true
    break
  fi
  sleep 5
done
test "$draft_ready" = true
draft_assets="$(jq -r '.assets[].name' /tmp/draft-release.json | LC_ALL=C sort)"
test "$draft_assets" = "$expected_assets"
jq -j '.body' /tmp/draft-release.json > /tmp/draft-release-notes.md
diff -u "$source_path/RELEASE_NOTES.md" /tmp/draft-release-notes.md

# Leave the identified draft untouched if the release tag or main
# moved while the draft was being inspected.
final_tag_commit="$(git ls-remote \
  "https://github.com/$GITHUB_REPOSITORY.git" \
  "refs/tags/$tag^{}" | cut -f1)"
test -n "$final_tag_commit"
test "$final_tag_commit" = "$expected_commit"
test "$GITHUB_SHA" = "$expected_commit"
# shellcheck source=scripts/gates.sh
. "$GITHUB_WORKSPACE/policy/scripts/gates.sh"
gate_main_matches "$expected_commit" "$GITHUB_REPOSITORY"

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
  if jq -e --arg tag "$tag" --argjson release_id "$release_id" \
    '.id == $release_id and .draft == false and .prerelease == false and .immutable == true
     and .tag_name == $tag and .name == $tag' \
    /tmp/published-release.json >/dev/null; then
    published_ready=true
    break
  fi
  sleep 5
done
test "$published_ready" = true
test "$(git ls-remote \
  "https://github.com/$GITHUB_REPOSITORY.git" \
  "refs/tags/$tag^{}" | cut -f1)" = "$expected_commit"
jq -j '.body' /tmp/published-release.json > /tmp/published-release-notes.md
diff -u "$source_path/RELEASE_NOTES.md" /tmp/published-release-notes.md
published_assets="$(jq -r '.assets[].name' /tmp/published-release.json | LC_ALL=C sort)"
test "$published_assets" = "$expected_assets"
jq -r '.assets[] | [.name, .digest] | @tsv' /tmp/published-release.json \
  | LC_ALL=C sort > /tmp/published-digests
diff -u /tmp/expected-digests /tmp/published-digests

latest_ready=false
for _ in 1 2 3 4 5; do
  if gh release list --repo "$GITHUB_REPOSITORY" --limit 100 \
    --json tagName,isLatest \
    | jq -e --arg tag "$tag" \
      'any(.[]; .tagName == $tag and .isLatest == true)' >/dev/null; then
    latest_ready=true
    break
  fi
  sleep 5
done
test "$latest_ready" = true

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
