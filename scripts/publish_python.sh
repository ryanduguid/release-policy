#!/usr/bin/env bash
# Publish one already-gated Python candidate without executing consumer code.
set -euo pipefail

if [ "$#" -ne 5 ]; then
  echo "usage: publish_python.sh TAG EXPECTED_COMMIT STEM VERSION SOURCE_PATH" >&2
  exit 64
fi

tag="$1"
expected_commit="$2"
base="$3-$4"
source_path="$5"
wheel="candidate/$base-py3-none-any.whl"
sdist="candidate/$base.tar.gz"
sbom="candidate/$base.spdx.json"
manifest="candidate/release-manifest.json"
checksums="candidate/SHA256SUMS"
assets=("$wheel" "$sdist" "$sbom" "$manifest" "$checksums")
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
gh api --method POST \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "repos/$GITHUB_REPOSITORY/releases" \
  --input /tmp/create-release.json \
  > /tmp/created-release.json
release_id="$(jq -er '.id | select(type == "number") | tostring' \
  /tmp/created-release.json)"
upload_url="https://uploads.github.com/repos/$GITHUB_REPOSITORY/releases/$release_id/assets"
jq -e \
  --argjson release_id "$release_id" \
  --arg tag "$tag" \
  '.id == $release_id and .draft == true and .prerelease == false
   and .tag_name == $tag and .name == $tag' \
  /tmp/created-release.json >/dev/null
jq -j '.body' /tmp/created-release.json > /tmp/created-release-notes.md
diff -u "$source_path/RELEASE_NOTES.md" /tmp/created-release-notes.md

upload_asset() {
  file="$1"
  media_type="$2"
  name="$(basename "$file")"
  [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]]
  gh api --method POST \
    -H "Content-Type: $media_type" \
    "$upload_url?name=$name" \
    --input "$file" \
    >/dev/null
}
upload_asset "$wheel" application/zip
upload_asset "$sdist" application/gzip
upload_asset "$sbom" application/spdx+json
upload_asset "$manifest" application/json
upload_asset "$checksums" text/plain

expected_assets="$(printf '%s\n' \
  "$base-py3-none-any.whl" \
  "$base.spdx.json" \
  "$base.tar.gz" \
  "release-manifest.json" \
  "SHA256SUMS" | LC_ALL=C sort)"
for file in "${assets[@]}"; do
  printf '%s\tsha256:%s\n' \
    "$(basename "$file")" \
    "$(sha256sum "$file" | cut -d' ' -f1)"
done | LC_ALL=C sort > /tmp/expected-digests

draft_ready=false
for _ in 1 2 3 4 5; do
  gh api \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "repos/$GITHUB_REPOSITORY/releases/$release_id" \
    > /tmp/draft-release.json
  jq -r '.assets[] | [.name, .digest] | @tsv' \
    /tmp/draft-release.json | LC_ALL=C sort > /tmp/draft-digests
  if jq -e \
      --argjson release_id "$release_id" \
      --arg tag "$tag" \
      '.id == $release_id and .draft == true and .prerelease == false
       and .tag_name == $tag and .name == $tag
       and (.assets | length) == 5
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

final_tag_commit="$(git ls-remote \
  "https://github.com/$GITHUB_REPOSITORY.git" \
  "refs/tags/$tag^{}" | cut -f1)"
test -n "$final_tag_commit"
test "$final_tag_commit" = "$expected_commit"
test "$GITHUB_SHA" = "$expected_commit"
# shellcheck source=scripts/gates.sh
. "$GITHUB_WORKSPACE/policy/scripts/gates.sh"
gate_main_matches "$expected_commit" "$GITHUB_REPOSITORY"
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
  jq -r '.assets[] | [.name, .digest] | @tsv' \
    /tmp/published-release.json | LC_ALL=C sort > /tmp/published-digests
  if jq -e \
      --argjson release_id "$release_id" \
      --arg tag "$tag" \
      '.id == $release_id and .draft == false and .prerelease == false
       and .immutable == true and .tag_name == $tag and .name == $tag
       and (.assets | length) == 5' \
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
published_assets="$(jq -r '.assets[].name' \
  /tmp/published-release.json | LC_ALL=C sort)"
test "$published_assets" = "$expected_assets"
jq -j '.body' /tmp/published-release.json > /tmp/published-release-notes.md
diff -u "$source_path/RELEASE_NOTES.md" /tmp/published-release-notes.md

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
