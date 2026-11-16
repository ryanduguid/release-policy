#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Runs .github/actions/secrets-scan/scan.sh over a scratch repository of pull
# requests, pushes and scheduled runs, and fails unless every case ends as
# expected. Needs the betterleaks release install.sh pins on PATH, on Linux.
# usage: test_secrets_scan.sh [a caller's .gitleaks.toml]
# Without an argument the base's .gitleaks.toml only extends the shared rules.
#
# Every identifier is fabricated: the ABN 98 765 432 100 fails the ABN
# checksum, and no caller may allowlist it, because the scan uses it as its
# canary.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
action="$HERE/../.github/actions/secrets-scan"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
if [ -n "${1:-}" ]; then
  cp "$1" "$scratch/caller.toml"
else
  printf '[extend]\npath = ".github/secrets-rules.toml"\n' > "$scratch/caller.toml"
fi
mkdir "$scratch/repo"
cd "$scratch/repo"

git init -q -b main .
git config user.name t
git config user.email t@example.invalid
git config commit.gpgsign false
git config advice.detachedHead false
git config core.autocrlf false
cp "$scratch/caller.toml" .gitleaks.toml
mkdir .github
cp "$action/rules.toml" .github/secrets-rules.toml
printf 'line 1\nline 2\nline 3\n' > notes.txt
printf 'top\nmid\nbottom\n' > h1.txt
git add -A
git commit -q -m base
base=$(git rev-parse HEAD)

commit_file() { # <file> <content> <message>
  printf '%s\n' "$2" > "$1"
  git add -A
  git commit -q -m "$3"
}

# (a) a commit holding a fabricated ABN reaches the branch through a merge.
git checkout -q -b side
commit_file supplier.txt 'Supplier ABN: 98 765 432 100' 'side: supplier'
git checkout -q -b pr-a "$base"
commit_file pr.txt 'pr work' 'pr: work'
git merge -q --no-ff -m 'merge side' side
head_a=$(git rev-parse HEAD)

# (b) both sides edit line 2 and the merge's resolution adds a TFN.
git checkout -q -b other "$base"
printf 'line 1\nline 2 other\nline 3\n' > notes.txt
git commit -q -am 'other: edit'
git checkout -q -b pr-b "$base"
printf 'line 1\nline 2 pr\nline 3\n' > notes.txt
git commit -q -am 'pr-b: edit'
git merge -q --no-ff other > /dev/null 2>&1 || true
printf 'line 1\nline 2 both, TFN 123 456 782\nline 3\n' > notes.txt
git commit -q -am 'merge other (resolved)'
head_b=$(git rev-parse HEAD)

# (c) a clean merge of main, which holds a finding of its own, into the branch.
git checkout -q main
commit_file ato.txt 'Supplier ABN: 98 765 432 100' 'main: supplier'
git checkout -q -b pr-c "$base"
commit_file c.txt 'c' 'pr-c: work'
git merge -q --no-ff -m 'merge main' main
head_c=$(git rev-parse HEAD)

# (d) 31 commits; the 31st holds the ABN.
git checkout -q -b pr-d "$base"
for n in $(seq 1 30); do commit_file "d$n.txt" "d $n" "pr-d: $n"; done
clean_d=$(git rev-parse HEAD)
commit_file d31.txt 'Supplier ABN: 98 765 432 100' 'pr-d: 31'
head_d=$(git rev-parse HEAD)

# (e) a merge of three parents that itself adds the ABN.
git checkout -q -b e1 "$base"
commit_file e1.txt 'e1' 'e1'
git checkout -q -b e2 "$base"
commit_file e2.txt 'e2' 'e2'
git checkout -q -b pr-e "$base"
commit_file e.txt 'e' 'pr-e: work'
git merge -q --no-ff --no-commit e1 e2
printf 'Supplier ABN: 98 765 432 100\n' > octopus.txt
git add -A
git commit -q -m 'merge e1 and e2, adding a file'
head_e=$(git rev-parse HEAD)

# (h) the branch adds the ABN in a commit; a later conflict on the same line is
# resolved by keeping that line unchanged.
git checkout -q -b other-h "$base"
printf 'top\nmid other\nbottom\n' > h1.txt
git commit -q -am 'other-h: edit'
git checkout -q -b pr-h "$base"
printf 'top\nSupplier ABN: 98 765 432 100\nbottom\n' > h1.txt
git commit -q -am 'pr-h: supplier'
commit_h=$(git rev-parse HEAD)
git merge -q --no-ff other-h > /dev/null 2>&1 || true
printf 'top\nSupplier ABN: 98 765 432 100\nbottom\n' > h1.txt
git commit -q -am 'merge other-h (kept ours)'
head_h=$(git rev-parse HEAD)

# Whose .gitleaks.toml, .gitleaksignore, inline allow comments and
# .gitattributes the gate honours. The base has no .gitleaksignore.
# (i) the pull request adds the ABN and allowlists its file in .gitleaks.toml.
git checkout -q -b pr-i "$base"
printf '%s\n' 'Supplier ABN: 98 765 432 100' > pr-i.txt
cat >> .gitleaks.toml <<'EOF'

[[allowlists]]
description = "Added by the pull request"
paths = ['''^pr-i\.txt$''']
EOF
git add -A
git commit -q -m 'pr-i: supplier, allowlisted in .gitleaks.toml'
head_i=$(git rev-parse HEAD)

# (j) the pull request adds the ABN, then its fingerprint in .gitleaksignore.
git checkout -q -b pr-j "$base"
commit_file pr-j.txt 'Supplier ABN: 98 765 432 100' 'pr-j: supplier'
commit_j=$(git rev-parse HEAD)
commit_file .gitleaksignore "$commit_j:pr-j.txt:au-business-number:1" 'pr-j: ignore it'
head_j=$(git rev-parse HEAD)

# (k) the pull request adds the ABN with an inline gitleaks:allow comment.
git checkout -q -b pr-k "$base"
commit_file pr-k.txt 'Supplier ABN: 98 765 432 100 # gitleaks:allow' 'pr-k: supplier, inline allow'
head_k=$(git rev-parse HEAD)

# (l) a base without .gitleaksignore and a clean pull request.
git checkout -q -b pr-l "$base"
commit_file pr-l.txt 'nothing to find' 'pr-l: work'
head_l=$(git rev-parse HEAD)

# (m) the pull request adds the ABN; its fingerprint reached the base first, in
# its own commit, and the branch then merged the base.
git checkout -q -b pr-m "$base"
commit_file pr-m.txt 'Supplier ABN: 98 765 432 100' 'pr-m: supplier'
commit_m=$(git rev-parse HEAD)
git checkout -q -b base-m "$base"
commit_file .gitleaksignore "$commit_m:pr-m.txt:au-business-number:1" 'base-m: ignore pr-m'
base_m=$(git rev-parse HEAD)
git checkout -q pr-m
git merge -q --no-ff -m 'merge base-m' base-m
head_m=$(git rev-parse HEAD)

# (n) a base without .gitleaks.toml; the pull request adds one, the mirror and
# a clean file.
git checkout -q --orphan base-n
git rm -q -r --cached . > /dev/null
git clean -q -fdx
commit_file readme.txt 'no config here' 'base-n: no configuration'
base_n=$(git rev-parse HEAD)
git checkout -q -b pr-n
cp "$scratch/caller.toml" .gitleaks.toml
mkdir .github
cp "$action/rules.toml" .github/secrets-rules.toml
git add -A
git commit -q -m 'pr-n: configuration'
head_n=$(git rev-parse HEAD)

# (o) the pull request edits .gitleaks.toml and adds nothing to find.
git checkout -q -b pr-o "$base"
printf '\n# A comment the pull request adds.\n' >> .gitleaks.toml
commit_file pr-o.txt 'nothing to find' 'pr-o: comment in the config'
head_o=$(git rev-parse HEAD)

# (p) an allowlist already in the base's .gitleaks.toml covers the ABN the pull
# request adds.
git checkout -q -b base-p "$base"
cat >> .gitleaks.toml <<'EOF'

[[allowlists]]
description = "Reviewed on the base"
paths = ['''^pr-p\.txt$''']
EOF
git add -A
git commit -q -m 'base-p: allowlist pr-p.txt'
base_p=$(git rev-parse HEAD)
git checkout -q -b pr-p
commit_file pr-p.txt 'Supplier ABN: 98 765 432 100' 'pr-p: supplier'
head_p=$(git rev-parse HEAD)

# (q) the pull request adds a .gitattributes that marks every file -diff, so a
# git log run in its checkout shows no text, and the ABN.
git checkout -q -b pr-q "$base"
printf '%s\n' '* -diff' > .gitattributes
commit_file pr-q.txt 'Supplier ABN: 98 765 432 100' 'pr-q: supplier, diff turned off'
head_q=$(git rev-parse HEAD)

# (s) the branch adds the ABN with an inline betterleaks:allow comment.
git checkout -q -b pr-s "$base"
commit_file pr-s.txt 'Supplier ABN: 98 765 432 100 # betterleaks:allow' 'pr-s: supplier, inline allow'
head_s=$(git rev-parse HEAD)

# (t) the branch adds the ABN, then its fingerprint in .betterleaksignore.
git checkout -q -b pr-t "$base"
commit_file pr-t.txt 'Supplier ABN: 98 765 432 100' 'pr-t: supplier'
commit_t=$(git rev-parse HEAD)
commit_file .betterleaksignore "$commit_t:pr-t.txt:au-business-number:1" 'pr-t: ignore it'
head_t=$(git rev-parse HEAD)

# (u) the branch adds the ABN and a .betterleaks.toml that allowlists its file.
git checkout -q -b pr-u "$base"
printf '%s\n' 'Supplier ABN: 98 765 432 100' > pr-u.txt
cat .gitleaks.toml > .betterleaks.toml
cat >> .betterleaks.toml <<'EOF'

[[allowlists]]
description = "Added by the branch"
paths = ['''^pr-u\.txt$''']
EOF
git add -A
git commit -q -m 'pr-u: supplier, allowlisted in .betterleaks.toml'
head_u=$(git rev-parse HEAD)

# The shared rules. (v) the pull request empties its mirror of them and adds
# the ABN: the scan runs the action's copy, and the mirror must equal it.
git checkout -q -b pr-v "$base"
printf '[extend]\nuseDefault = true\n' > .github/secrets-rules.toml
commit_file pr-v.txt 'Supplier ABN: 98 765 432 100' 'pr-v: supplier, mirror emptied'
head_v=$(git rev-parse HEAD)

# (w) the pull request edits only a comment in its mirror.
git checkout -q -b pr-w "$base"
printf '\n# A comment the pull request adds.\n' >> .github/secrets-rules.toml
git commit -q -am 'pr-w: comment in the mirror'
head_w=$(git rev-parse HEAD)

# (x) the pull request's .gitleaks.toml stops extending the shared rules.
git checkout -q -b pr-x "$base"
printf '[extend]\nuseDefault = true\n' > .gitleaks.toml
git commit -q -am 'pr-x: default rules only'
head_x=$(git rev-parse HEAD)

# (y) the base's .gitleaks.toml does not extend the shared rules.
git checkout -q -b base-y "$base"
printf '[extend]\nuseDefault = true\n' > .gitleaks.toml
git commit -q -am 'base-y: default rules only'
base_y=$(git rev-parse HEAD)
git checkout -q -b pr-y
git checkout -q "$base" -- .gitleaks.toml
commit_file pr-y.txt 'nothing to find' 'pr-y: extend the shared rules again'
head_y=$(git rev-parse HEAD)

# (z) the pull request's .gitleaks.toml allowlists the canary ABN.
git checkout -q -b pr-z "$base"
cat >> .gitleaks.toml <<'EOF'

[[allowlists]]
description = "Added by the pull request"
regexes = ['''^98 765 432 100$''']
EOF
git commit -q -am 'pr-z: allowlist the canary'
head_z=$(git rev-parse HEAD)

# A file Git takes for binary: one NUL byte after the ABN.
git checkout -q -b pr-nul "$base"
printf 'Supplier ABN: 98 765 432 100\n\0\n' > pr-nul.txt
git add -A
git commit -q -m 'pr-nul: supplier, then a NUL byte'
head_nul=$(git rev-parse HEAD)

# A lone AWS access key ID, assembled here so no scan of this file reports it.
# betterleaks' default rule wants a secret key beside it; rules.toml clears
# that, so the key alone is a finding.
git checkout -q -b pr-aws "$base"
commit_file pr-aws.txt "aws_access_key_id = AKIA$(printf 'QWERTYUIOP%s' ASDFGH)" 'pr-aws: a key ID alone'
head_aws=$(git rev-parse HEAD)

# The merge commit GitHub builds for pr-l once the base has moved to base-p,
# whose .gitleaks.toml the pull request's own head does not hold.
git checkout -q -b merge-l "$base_p"
git merge -q --no-ff -m 'merge pr-l' pr-l
merge_l=$(git rev-parse HEAD)

# (r) and (r2) put a git first on PATH that fails one subcommand. scan.sh runs
# no git log itself, so (r) is the scanner's own git log failing; (r2) fails
# the ls-tree that looks for the commit's .gitleaksignore.
real_git=$(command -v git)
fake_git() { # <folder> <subcommand>
  mkdir "$1"
  cat > "$1/git" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do [ "\$a" = $2 ] && { echo 'injected: git $2 failed' >&2; exit 1; }; done
exec "$real_git" "\$@"
EOF
  chmod +x "$1/git"
}
fakebin="$scratch/fakebin"
fake_git "$fakebin" log
fakebin_tree="$scratch/fakebin-tree"
fake_git "$fakebin_tree" ls-tree

# Each case runs with the head checked out, as actions/checkout leaves a pull
# request, so a scan that read the work tree would read the head's files.
# The event scan's configuration comes from the head unless CONFIG names the
# merge commit.
# <expect> is leak (fails with a finding), clean-fail (fails with no finding)
# or pass.
mismatches=0
run() { # <label> <base> <head> <expect> [event] [path prefix]
  local code tmp findings verdict matched=0 out="$scratch/out-$1.txt"
  git checkout -q -f --detach "$3" 2> /dev/null || true
  tmp=$(mktemp -d)
  set +e
  PATH="${6:+$6:}$PATH" EVENT_NAME=${5:-pull_request} BASE_SHA=$2 HEAD_SHA=$3 CONFIG_SHA=${CONFIG:-$3} \
    RUNNER_TEMP=$tmp bash "$action/scan.sh" > "$out" 2>&1
  code=$?
  set -e
  rm -rf "$tmp"
  findings=$(grep -c 'git\.sha \.' "$out" || true)
  case "$4" in
    leak) [ "$code" -ne 0 ] && [ "$findings" -gt 0 ] && matched=1 ;;
    clean-fail) [ "$code" -ne 0 ] && [ "$findings" -eq 0 ] && matched=1 ;;
    pass) [ "$code" -eq 0 ] && matched=1 ;;
  esac
  if [ "$matched" -eq 1 ]; then
    verdict="ok($4)"
  else
    verdict="MISMATCH(expected $4)"
    mismatches=$((mismatches + 1))
  fi
  printf '%-38s exit=%s findings=%s %s %s\n' "$1" "$code" "$findings" "$verdict" \
    "$(sed 's/\x1b\[[0-9;]*m//g' "$out" | grep -E '^::error::|fatal' | cut -c1-110 | tr '\n' '|' || true)"
}

echo "git $(git --version | awk '{print $3}'), betterleaks $(betterleaks version)"
run a-merged-in-commit "$base" "$head_a" leak
run b-merge-resolution "$base" "$head_b" leak
# With the base main had when the branch was cut, main's own commit is in range;
# with main's tip as the base, only the branch's commits and the merge are.
run c-clean-merge-stale-base "$base" "$head_c" leak
run c-clean-merge-fresh-base "$(git rev-parse main)" "$head_c" pass
run d-31-commits "$base" "$head_d" leak
run e-three-parent-merge "$base" "$head_e" clean-fail
run f-same-sha "$head_a" "$head_a" clean-fail
run f-head-behind-base "$head_a" "$base" clean-fail
run g-unknown-base 0123456789012345678901234567890123456789 "$head_a" clean-fail
run g-empty-sha '' "$head_a" clean-fail
run h-kept-line-no-ignore "$base" "$head_h" leak
# The fingerprint sits only in the work tree, as if the pull request had
# added it.
git checkout -q -f --detach "$head_h"
printf '%s:h1.txt:au-business-number:2\n' "$commit_h" > .gitleaksignore
run h-kept-line-fingerprint-ignored "$base" "$head_h" leak
rm -f .gitleaksignore
run i-pr-toml-allowlists-its-abn "$base" "$head_i" leak
run j-pr-ignore-fingerprints-abn "$base" "$head_j" leak
run k-pr-inline-gitleaks-allow "$base" "$head_k" leak
run l-base-no-ignore-clean-pr "$base" "$head_l" pass
run m-base-ignore-fingerprints-pr "$base_m" "$head_m" pass
run n-base-no-toml "$base_n" "$head_n" clean-fail
run o-pr-edits-toml-clean "$base" "$head_o" pass
run p-base-toml-allowlists-pr "$base_p" "$head_p" pass
run q-pr-gitattributes-no-diff "$base" "$head_q" leak
run r-injected-scan-git-failure "$base" "$head_l" clean-fail pull_request "$fakebin"
run r2-injected-ls-tree-failure "$base" "$head_l" clean-fail pull_request "$fakebin_tree"
run s-pr-inline-betterleaks-allow "$base" "$head_s" leak
run t-pr-betterleaksignore-fingerprint "$base" "$head_t" leak
run u-pr-betterleaks-toml-allowlists "$base" "$head_u" leak
run v-pr-empties-mirror "$base" "$head_v" leak
run w-pr-mirror-differs "$base" "$head_w" clean-fail
run x-pr-toml-drops-shared-rules "$base" "$head_x" clean-fail
run y-base-toml-lacks-shared-rules "$base_y" "$head_y" clean-fail
run z-pr-toml-allowlists-canary "$base" "$head_z" clean-fail
run nul-byte-after-abn "$base" "$head_nul" leak
run lone-aws-key-id "$base" "$head_aws" leak
CONFIG=$merge_l run merge-commit-config "$base_p" "$head_l" pass
run push-adds-abn "$clean_d" "$head_d" leak push
run push-nul-byte-after-abn "$base" "$head_nul" leak push
run push-clean "$base" "$clean_d" pass push
run push-three-parent-merge "$base" "$head_e" clean-fail push
run push-mirror-differs "$base" "$head_w" clean-fail push
run schedule-full-history '' "$head_l" leak schedule
run dispatch-full-history '' "$head_l" leak workflow_dispatch
run target-event-gate "$base" "$head_i" leak pull_request_target
run target-event-clean "$base" "$head_l" pass pull_request_target
run unsupported-event "$base" "$head_l" clean-fail issue_comment
echo "mismatches=$mismatches"
[ "$mismatches" -eq 0 ]
