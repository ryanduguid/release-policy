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
  if [ "$status" -ne 0 ] && [ "$published" != true ]; then
    # `gh release create` uploads the assets in the same call, so it
    # can leave a draft behind and still fail before its id reached
    # release_id. Recovering the id from the tag alone carries a
    # residual risk: if `gh release create` fails before creating
    # anything and a draft with this tag_name, name, draft and
    # prerelease already exists, this deletes a draft the run did not
    # create. gate_no_existing_release enumerates drafts in the
    # immediately preceding step and the `concurrency` group
    # serialises same-tag runs, so that window is seconds wide, but it
    # is not closed. The sibling trap in release-python.yml avoids the
    # window by gating on a captured id. Adopting the create-then-
    # upload sequence that makes that possible here retires
    # find_created_draft_release.py, so the PR raises it as a
    # follow-up. A second window stays open in the other direction: a
    # draft whose requested tag has not settled still reports a
    # tag_name carrying the `untagged-` prefix, which matches nothing
    # below, so the trap refuses and leaves that draft to block the
    # retries this recovery exists to unblock. Anything other than
    # exactly one match, including an empty or unparseable listing,
    # leaves the id empty and the refusal stands.
    if [ -z "$release_id" ] \
      && gh api --paginate \
        -H "X-GitHub-Api-Version: 2026-03-10" \
        "repos/$GITHUB_REPOSITORY/releases" \
        > /tmp/cleanup-releases.json; then
      release_id="$(jq -rs --arg tag "$tag" \
        '[(add // [])[] | select(.draft == true and .prerelease == false
                                 and .tag_name == $tag and .name == $tag)]
         | if length == 1 then (.[0].id | tostring) else "" end' \
        /tmp/cleanup-releases.json)" || release_id=""
    fi
    if [ -n "$release_id" ] \
      && gh api \
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

created_release_url="$(gh release create "$tag" \
  --repo "$GITHUB_REPOSITORY" \
  --verify-tag \
  --draft \
  --title "$tag" \
  --notes-file "$source_path/RELEASE_NOTES.md" \
  "$zip#Deterministic source ZIP" \
  "$tar#Deterministic source tar archive" \
  "$sbom#SPDX 2.3 SBOM" \
  "$dist/SHA256SUMS#SHA-256 checksums")"

release_id="$(python "$GITHUB_WORKSPACE/policy/scripts/find_created_draft_release.py" \
  --repository "$GITHUB_REPOSITORY" \
  --created-url "$created_release_url" \
  --expected-tag "$tag" \
  --attempts 5 \
  --delay-seconds 5)"

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
  if jq -e --arg tag "$tag" \
      '.draft == true and .prerelease == false
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
  if jq -e --arg tag "$tag" \
    '.draft == false and .prerelease == false and .immutable == true
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
