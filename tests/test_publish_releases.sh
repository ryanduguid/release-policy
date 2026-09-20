#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY="$(cd "$HERE/.." && pwd)"
FIXTURE="$(mktemp -d)"
trap 'rm -rf "$FIXTURE"' EXIT
export FIXTURE
family="${1:-archives}"
case "$family" in
  archives) asset_names=(example-1.2.3.zip example-1.2.3.tar.gz example-1.2.3.spdx.json SHA256SUMS) ;;
  python) asset_names=(example-1.2.3-py3-none-any.whl example-1.2.3.tar.gz example-1.2.3.spdx.json release-manifest.json SHA256SUMS) ;;
  *) exit 64 ;;
esac
mkdir -p "$FIXTURE/consumer/dist" "$FIXTURE/workspace"
ln -s "$POLICY" "$FIXTURE/workspace/policy"
printf 'Release notes.\n' > "$FIXTURE/consumer/RELEASE_NOTES.md"
for name in "${asset_names[@]}"; do
  printf '%s\n' "$name" > "$FIXTURE/consumer/dist/$name"
done
cd "$FIXTURE/consumer"
ln -s dist candidate
export GH=gh
export GITHUB_REPOSITORY=example/fixture GITHUB_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export GITHUB_WORKSPACE="$FIXTURE/workspace"

# Every GitHub request is handled here; an unexpected request fails closed.
gh() {
  printf '%s\n' "$*" >> "$FIXTURE/calls"
  local method=GET endpoint="" input="" media="" arg
  if [ "$1 $2" = "release create" ]; then return 1; fi
  if [ "$1 $2" = "release list" ]; then
    if [ "$CASE" = superseded ]; then
      printf '[{"tagName":"v1.2.3","isLatest":false},{"tagName":"sibling/v2.0.0","isLatest":true}]\n'
    else
      printf '[{"tagName":"v1.2.3","isLatest":true}]\n'
    fi
    return
  fi
  if [ "$1 $2" = "release verify" ] || [ "$1 $2" = "release verify-asset" ]; then
    [ "$CASE" != verification-fails ] || return 1
    if [ "$1 $2" = "release verify-asset" ]; then
      [ "$CASE" != asset-verification-fails ] || return 1
    fi
    return 0
  fi
  for ((arg=1; arg<=$#; arg++)); do
    if [ "${!arg}" = --method ]; then
      arg=$((arg+1)); method="${!arg}"
    elif [ "${!arg}" = --input ]; then
      arg=$((arg+1)); input="${!arg}"
    elif [[ "${!arg}" == 'Content-Type: '* ]]; then
      media="${!arg}"
    elif [[ "${!arg}" == repos/* || "${!arg}" == https://uploads.github.com/* ]]; then
      endpoint="${!arg}"
    fi
  done
  case "$method $endpoint" in
    'POST repos/example/fixture/releases')
      [ "$CASE" != create-fails ] || return 1
      jq -e --rawfile body "$FIXTURE/consumer/RELEASE_NOTES.md" \
        '. == {tag_name:"v1.2.3",name:"v1.2.3",draft:true,prerelease:false,body:$body}' \
        "$input" >/dev/null || return 1
      local created_tag=v1.2.3
      case "$CASE" in
        settles|never-settles) created_tag=untagged-pending ;;
        changed-tag) created_tag=v9.9.9 ;;
      esac
      local notes="$FIXTURE/consumer/RELEASE_NOTES.md"
      [ "$CASE" != notes ] || notes="$FIXTURE/created-notes"
      # Native jq reads --rawfile in text mode; preserve CRLF and final newlines.
      local body
      body="$(cat "$notes"; printf .)"
      jq -n --arg tag "$created_tag" --arg body "${body%.}" \
        '{id:7,tag_name:$tag,name:"v1.2.3",draft:true,prerelease:false,body:$body}'
      ;;
    POST\ https://uploads.github.com/repos/example/fixture/releases/7/assets\?*)
      # The ID must be known even when the first upload fails.
      [[ "$CASE" = success || "$CASE" = settles || "$CASE" = superseded || "$CASE" = notes || "$CASE" = published-* || "$CASE" = *verification-fails ]] || return 1
      local name="${endpoint#*name=}" expected_media
      name="${name%%&*}"
      case "$name" in
        example-1.2.3.zip|example-1.2.3-py3-none-any.whl) expected_media=application/zip ;;
        example-1.2.3.tar.gz) expected_media=application/gzip ;;
        example-1.2.3.spdx.json) expected_media=application/spdx+json ;;
        release-manifest.json) expected_media=application/json ;;
        SHA256SUMS) expected_media=text/plain ;;
        *) return 1 ;;
      esac
      [[ "$input" = "$FIXTURE/consumer/dist/$name" || "$input" = "candidate/$name" ]] || return 1
      [ "$media" = "Content-Type: $expected_media" ] || return 1
      printf '%s\n' "$endpoint" >> "$FIXTURE/uploads"
      ;;
    'PATCH repos/example/fixture/releases/7')
      touch "$FIXTURE/published"
      ;;
    'GET repos/example/fixture/git/ref/heads/main')
      printf '%s\n' "$GITHUB_SHA"
      ;;
    'GET repos/example/fixture/releases/7')
      if [ "$CASE" = never-settles ] || { [ "$CASE" = settles ] && [ ! -f "$FIXTURE/seen-unsettled" ]; }; then
        touch "$FIXTURE/seen-unsettled"
        printf '{"id":7,"tag_name":"untagged-pending","name":"v1.2.3","draft":true,"prerelease":false}\n'
        return
      fi
      case "$CASE" in
        success|settles|superseded|notes|published-*|*verification-fails)
          local draft=true
          [ ! -f "$FIXTURE/published" ] || draft=false
          local notes="$FIXTURE/consumer/RELEASE_NOTES.md"
          if [ "$CASE" = notes ]; then
            notes="$FIXTURE/draft-notes"
            [ "$draft" = true ] || notes="$FIXTURE/published-notes"
          fi
          local body
          body="$(cat "$notes"; printf .)"
          jq -n --arg case "$CASE" --argjson draft "$draft" --slurpfile assets "$FIXTURE/assets" \
            --arg body "${body%.}" \
            '{id:7,tag_name:"v1.2.3",name:"v1.2.3",draft:$draft,prerelease:false,immutable:true,body:$body,assets:$assets}
             | if $draft then .
               elif $case == "published-mutable" then .immutable = false
               elif $case == "published-identity" then .id = 8
               elif $case == "published-digest" then .assets[0].digest = "sha256:wrong"
               elif $case == "published-notes" then .body = "changed"
               else . end'
          ;;
        already-published) printf '{"id":7,"tag_name":"v1.2.3","name":"v1.2.3","draft":false,"prerelease":false}\n' ;;
        wrong-identity) printf '{"id":8,"tag_name":"v1.2.3","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
        changed-tag) printf '{"id":7,"tag_name":"v9.9.9","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
        unsettled-tag) printf '{"id":7,"tag_name":"untagged-pending","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
        *) printf '{"id":7,"tag_name":"v1.2.3","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
      esac
      ;;
    'GET repos/example/fixture/releases?per_page=100') printf '7\n' ;;
    'GET repos/example/fixture/releases')
      printf '[{"id":99,"tag_name":"v1.2.3","name":"v1.2.3","draft":true,"prerelease":false}]\n'
      ;;
    'GET repos/example/fixture/releases/99')
      printf '{"id":99,"tag_name":"v1.2.3","name":"v1.2.3","draft":true,"prerelease":false}\n'
      ;;
    DELETE\ *) printf '%s\n' "$endpoint" >> "$FIXTURE/deleted" ;;
    *) echo "unexpected GitHub request: $*" >&2; return 1 ;;
  esac
}
export -f gh
git() {
  if [ "$1" = ls-remote ]; then
    [ "$CASE" != missing-tag ] || return 0
    if [ "$CASE" = moved-tag ]; then printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\trefs/tags/v1.2.3^{}\n';
    else printf '%s\trefs/tags/v1.2.3^{}\n' "$GITHUB_SHA"; fi
  else echo "unexpected git request" >&2; return 1; fi
}
export -f git
sleep() { printf '%s\n' "$*" >> "$FIXTURE/sleeps"; }
export -f sleep

failures=0
if [ "$family" = archives ]; then
for CASE in create-fails upload-fails already-published wrong-identity unsettled-tag missing-tag moved-tag changed-tag never-settles; do
  export CASE
  : > "$FIXTURE/calls"
  : > "$FIXTURE/deleted"
  : > "$FIXTURE/sleeps"
  if bash "$POLICY/scripts/publish_$family.sh" v1.2.3 "$GITHUB_SHA" example 1.2.3 \
      "$FIXTURE/consumer" > "$FIXTURE/output" 2>&1; then
    echo "FAIL $CASE: publication unexpectedly succeeded"
    failures=$((failures+1))
  fi
  expected=""
  [ "$CASE" != upload-fails ] || expected=repos/example/fixture/releases/7
  if [ "$(cat "$FIXTURE/deleted")" != "$expected" ]; then
    echo "FAIL $CASE: unexpected draft cleanup"
    cat "$FIXTURE/calls" "$FIXTURE/output"
    failures=$((failures+1))
  else
    echo "ok   $CASE preserves draft ownership"
  fi
  if [[ "$CASE" = missing-tag || "$CASE" = moved-tag ]] && [ -s "$FIXTURE/calls" ]; then
    echo "FAIL $CASE: GitHub was called after tag verification failed"
    failures=$((failures+1))
  fi
  if [ "$CASE" = changed-tag ] && { [ -s "$FIXTURE/sleeps" ] || grep -q 'uploads.github.com' "$FIXTURE/calls"; }; then
    echo "FAIL changed-tag: retried or uploaded after an identity change"
    failures=$((failures+1))
  fi
  if [ "$CASE" = never-settles ] && { [ "$(wc -l < "$FIXTURE/sleeps" | tr -d ' ')" != 4 ] || grep -q 'uploads.github.com' "$FIXTURE/calls"; }; then
    echo "FAIL never-settles: expected four retry delays without uploads"
    failures=$((failures+1))
  fi
done

fi
cases=(success superseded published-mutable published-identity published-digest published-notes verification-fails asset-verification-fails)
[ "$family" != archives ] || cases+=(settles)
for CASE in "${cases[@]}"; do
export CASE
rm -f "$FIXTURE/published" "$FIXTURE/seen-unsettled"
: > "$FIXTURE/calls"
: > "$FIXTURE/assets"
: > "$FIXTURE/uploads"
: > "$FIXTURE/deleted"
for name in "${asset_names[@]}"; do
  digest="$(sha256sum "$FIXTURE/consumer/dist/$name" | cut -d' ' -f1)"
  jq -n --arg name "$name" --arg digest "sha256:$digest" '{name:$name,digest:$digest}' >> "$FIXTURE/assets"
done
status=0
bash "$POLICY/scripts/publish_$family.sh" v1.2.3 "$GITHUB_SHA" example 1.2.3 \
    "$FIXTURE/consumer" > "$FIXTURE/output" 2>&1 || status=$?
expected_status=0
[[ "$CASE" != published-* && "$CASE" != *verification-fails ]] || expected_status=1
if [ "$status" -eq "$expected_status" ] \
    && [ -f "$FIXTURE/published" ] && [ ! -s "$FIXTURE/deleted" ] \
    && [ "$(sort -u "$FIXTURE/uploads" | wc -l | tr -d ' ')" = "${#asset_names[@]}" ]; then
  echo "ok   $family $CASE checks all ${#asset_names[@]} assets without deleting a published release"
else
  echo "FAIL $CASE: publication or asset inventory failed"
  cat "$FIXTURE/output"
  failures=$((failures+1))
fi
if [ "$expected_status" = 0 ]; then
  if [ "$(grep -c '^release verify ' "$FIXTURE/calls")" != 1 ] \
      || [ "$(grep -c '^release verify-asset ' "$FIXTURE/calls")" != "${#asset_names[@]}" ]; then
    echo "FAIL $family $CASE: release or asset verification was skipped"
    failures=$((failures+1))
  fi
fi
done
# Use byte-exact fixtures so checkout settings and native jq output cannot hide
# newline differences. Check each verification stage, including failures that
# must reach the draft or published release before rejecting the changed body.
CASE=notes
export CASE
for stage in created draft published; do
  for variant in crlf-source crlf-body content space tab trailing-space added-blank removed-blank added-newline removed-newline embedded-cr final-cr; do
    printf 'Release notes.\n\nEnd.' > "$FIXTURE/consumer/RELEASE_NOTES.md"
    for notes_stage in created draft published; do
      cp "$FIXTURE/consumer/RELEASE_NOTES.md" "$FIXTURE/$notes_stage-notes"
    done
    expected_status=1
    case "$variant" in
      crlf-source)
        printf 'Release notes.\r\n\r\nEnd.' > "$FIXTURE/consumer/RELEASE_NOTES.md"
        expected_status=0 ;;
      crlf-body)
        printf 'Release notes.\r\n\r\nEnd.' > "$FIXTURE/$stage-notes"
        expected_status=0 ;;
      content) printf 'Changed notes.\n\nEnd.' > "$FIXTURE/$stage-notes" ;;
      space) printf 'Release  notes.\n\nEnd.' > "$FIXTURE/$stage-notes" ;;
      tab) printf 'Release\tnotes.\n\nEnd.' > "$FIXTURE/$stage-notes" ;;
      trailing-space) printf 'Release notes. \n\nEnd.' > "$FIXTURE/$stage-notes" ;;
      added-blank) printf 'Release notes.\n\n\nEnd.' > "$FIXTURE/$stage-notes" ;;
      removed-blank) printf 'Release notes.\nEnd.' > "$FIXTURE/$stage-notes" ;;
      added-newline) printf 'Release notes.\n\nEnd.\n' > "$FIXTURE/$stage-notes" ;;
      removed-newline)
        printf 'Release notes.\n\nEnd.\n' > "$FIXTURE/consumer/RELEASE_NOTES.md"
        for notes_stage in created draft published; do
          [ "$notes_stage" = "$stage" ] || cp "$FIXTURE/consumer/RELEASE_NOTES.md" "$FIXTURE/$notes_stage-notes"
        done ;;
      embedded-cr) printf 'Release\r notes.\n\nEnd.' > "$FIXTURE/$stage-notes" ;;
      final-cr) printf 'Release notes.\n\nEnd.\r' > "$FIXTURE/$stage-notes" ;;
    esac
    rm -f "$FIXTURE/published"
    : > "$FIXTURE/calls"
    : > "$FIXTURE/deleted"
    : > "$FIXTURE/uploads"
    status=0
    bash "$POLICY/scripts/publish_$family.sh" v1.2.3 "$GITHUB_SHA" example 1.2.3 \
      "$FIXTURE/consumer" > "$FIXTURE/output" 2>&1 || status=$?
    if [ "$expected_status" = 0 ]; then
      if [ "$status" = 0 ] && [ -f "$FIXTURE/published" ] && [ ! -s "$FIXTURE/deleted" ] \
          && [ "$(grep -c '^release verify ' "$FIXTURE/calls")" = 1 ] \
          && [ "$(grep -c '^release verify-asset ' "$FIXTURE/calls")" = "${#asset_names[@]}" ]; then
        echo "ok   $family $stage $variant verifies the release and every asset"
        continue
      fi
    elif [ "$status" = 1 ] && grep -q "/tmp/$stage-release-notes.md" "$FIXTURE/output"; then
      if { [ "$stage" = published ] && [ -f "$FIXTURE/published" ] && [ ! -s "$FIXTURE/deleted" ]; } \
          || { [ "$stage" != published ] && [ ! -f "$FIXTURE/published" ] \
            && [ "$(cat "$FIXTURE/deleted")" = repos/example/fixture/releases/7 ]; }; then
        echo "ok   $family $stage $variant rejects changed notes at the expected stage"
        continue
      fi
    fi
    echo "FAIL $family $stage $variant: release-note verification or cleanup was incorrect"
    cat "$FIXTURE/output"
    failures=$((failures+1))
  done
done

test "$failures" -eq 0
