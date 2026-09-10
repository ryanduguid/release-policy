#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY="$(cd "$HERE/.." && pwd)"
FIXTURE="$(mktemp -d)"
trap 'rm -rf "$FIXTURE"' EXIT
export FIXTURE
mkdir -p "$FIXTURE/consumer/dist" "$FIXTURE/workspace"
ln -s "$POLICY" "$FIXTURE/workspace/policy"
printf 'Release notes.\n' > "$FIXTURE/consumer/RELEASE_NOTES.md"
for name in example-1.2.3.zip example-1.2.3.tar.gz example-1.2.3.spdx.json SHA256SUMS; do
  printf '%s\n' "$name" > "$FIXTURE/consumer/dist/$name"
done
export GH=gh
export GITHUB_REPOSITORY=example/fixture GITHUB_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export GITHUB_WORKSPACE="$FIXTURE/workspace"

# Every GitHub request is handled here; an unexpected request fails closed.
gh() {
  printf '%s\n' "$*" >> "$FIXTURE/calls"
  local method=GET endpoint="" input="" media="" arg
  if [ "$1 $2" = "release create" ]; then return 1; fi
  if [ "$1 $2" = "release list" ]; then
    printf '[{"tagName":"v1.2.3","isLatest":true}]\n'; return
  fi
  if [ "$1 $2" = "release verify" ] || [ "$1 $2" = "release verify-asset" ]; then
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
      jq -n --arg tag "$created_tag" --rawfile body "$FIXTURE/consumer/RELEASE_NOTES.md" \
        '{id:7,tag_name:$tag,name:"v1.2.3",draft:true,prerelease:false,body:$body}'
      ;;
    POST\ https://uploads.github.com/repos/example/fixture/releases/7/assets\?*)
      # The ID must be known even when the first upload fails.
      [[ "$CASE" = success || "$CASE" = settles ]] || return 1
      local name="${endpoint#*name=}" expected_media
      name="${name%%&*}"
      case "$name" in
        example-1.2.3.zip) expected_media=application/zip ;;
        example-1.2.3.tar.gz) expected_media=application/gzip ;;
        example-1.2.3.spdx.json) expected_media=application/spdx+json ;;
        SHA256SUMS) expected_media=text/plain ;;
        *) return 1 ;;
      esac
      [ "$input" = "$FIXTURE/consumer/dist/$name" ] || return 1
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
        success|settles)
          local draft=true
          [ ! -f "$FIXTURE/published" ] || draft=false
          jq -n --argjson draft "$draft" --slurpfile assets "$FIXTURE/assets" \
            --rawfile body "$FIXTURE/consumer/RELEASE_NOTES.md" \
            '{id:7,tag_name:"v1.2.3",name:"v1.2.3",draft:$draft,prerelease:false,immutable:true,body:$body,assets:$assets}'
          ;;
        already-published) printf '{"id":7,"tag_name":"v1.2.3","name":"v1.2.3","draft":false,"prerelease":false}\n' ;;
        wrong-identity) printf '{"id":8,"tag_name":"v1.2.3","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
        changed-tag) printf '{"id":7,"tag_name":"v9.9.9","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
        unsettled-tag) printf '{"id":7,"tag_name":"untagged-pending","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
        *) printf '{"id":7,"tag_name":"v1.2.3","name":"v1.2.3","draft":true,"prerelease":false}\n' ;;
      esac
      ;;
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
for CASE in create-fails upload-fails already-published wrong-identity unsettled-tag missing-tag moved-tag changed-tag never-settles; do
  export CASE
  : > "$FIXTURE/calls"
  : > "$FIXTURE/deleted"
  : > "$FIXTURE/sleeps"
  if bash "$POLICY/scripts/publish_archives.sh" v1.2.3 "$GITHUB_SHA" example 1.2.3 \
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

for CASE in success settles; do
rm -f "$FIXTURE/published" "$FIXTURE/seen-unsettled"
: > "$FIXTURE/assets"
: > "$FIXTURE/uploads"
: > "$FIXTURE/deleted"
for name in example-1.2.3.zip example-1.2.3.tar.gz example-1.2.3.spdx.json SHA256SUMS; do
  digest="$(sha256sum "$FIXTURE/consumer/dist/$name" | cut -d' ' -f1)"
  jq -n --arg name "$name" --arg digest "sha256:$digest" '{name:$name,digest:$digest}' >> "$FIXTURE/assets"
done
if bash "$POLICY/scripts/publish_archives.sh" v1.2.3 "$GITHUB_SHA" example 1.2.3 \
    "$FIXTURE/consumer" > "$FIXTURE/output" 2>&1 \
    && [ -f "$FIXTURE/published" ] && [ ! -s "$FIXTURE/deleted" ] \
    && [ "$(sort -u "$FIXTURE/uploads" | wc -l | tr -d ' ')" = 4 ]; then
  echo "ok   $CASE publishes all four verified assets"
else
  echo "FAIL $CASE: publication or asset inventory failed"
  cat "$FIXTURE/output"
  failures=$((failures+1))
fi
done
test "$failures" -eq 0
