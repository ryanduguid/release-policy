#!/bin/sh
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Install one release that release-binary.yml published, on Linux or macOS:
#
#   sh install.sh [--modify-path] [--] OWNER/REPO TAG [DIRECTORY]
#
# It downloads SHA256SUMS, this platform's archive and the provenance bundle
# from the release, then installs nothing unless the GitHub CLI verifies
# SHA256SUMS and the archive against that bundle, as signed by release-policy's
# release-binary.yml for OWNER/REPO at TAG on a GitHub-hosted runner, and the
# archive matches its line in SHA256SUMS. The archive's files go into
# DIRECTORY, by default ${XDG_DATA_HOME:-$HOME/.local/share}/<stem>. With
# --modify-path it then appends one line that adds DIRECTORY to PATH to the
# startup file of the login shell $SHELL names, unless that file already holds
# the line. Every step runs inside main, so a truncated download of this script
# runs nothing.
set -eu

SIGNER=ryanduguid/release-policy/.github/workflows/release-binary.yml

refuse() {
  echo "install.sh: $*" >&2
  exit 1
}

# Sets target from the running platform; any other platform has no build.
pick_target() {
  os=$(uname -s) arch=$(uname -m)
  case "$os/$arch" in
    Linux/x86_64 | Linux/amd64) target=x86_64-unknown-linux-musl ;;
    Linux/aarch64 | Linux/arm64) target=aarch64-unknown-linux-musl ;;
    # A shell under Rosetta runs uname translated, so it reports x86_64 on
    # Apple silicon; sysctl, translated too, says so, and arm64 runs natively.
    Darwin/x86_64)
      if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]; then
        target=aarch64-apple-darwin
      else
        target=x86_64-apple-darwin
      fi ;;
    Darwin/arm64) target=aarch64-apple-darwin ;;
    *) refuse "no build for $os $arch: releases ship Linux and macOS x86_64 and arm64 here, Windows through install.ps1" ;;
  esac
}

# Sets profile, the startup file of the login shell $SHELL names, and refuses
# before anything is downloaded unless it can be written.
pick_profile() {
  shell=${SHELL:-}
  shell=${shell##*/}
  case "$shell" in
    # ~/.zshenv often sets ZDOTDIR without exporting it; zsh -c reads that file.
    zsh)
      zdotdir=${ZDOTDIR:-}
      if [ -z "$zdotdir" ]; then
        # shellcheck disable=SC2016 # zsh expands ZDOTDIR.
        zdotdir=$("$SHELL" -c 'printf %s "$ZDOTDIR"' 2>/dev/null < /dev/null) || zdotdir=
        # Text ~/.zshenv prints comes first; take only an absolute directory.
        case "$zdotdir" in /*) ;; *) zdotdir= ;; esac
      fi
      profile=${zdotdir:-$HOME}/.zshrc ;;
    # macOS terminals start bash as a login shell, which skips .bashrc.
    bash) if [ "$os" = Darwin ]; then profile=$HOME/.bash_profile; else profile=$HOME/.bashrc; fi ;;
    fish) profile=${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish ;;
    *) profile=$HOME/.profile ;;
  esac
  # Appending through a link would change a file the user was not told of.
  [ ! -L "$profile" ] \
    || refuse "--modify-path will not write through the symbolic link $profile; nothing was installed"
  if [ -e "$profile" ]; then
    [ -f "$profile" ] && [ -w "$profile" ] \
      || refuse "--modify-path cannot write to $profile; nothing was installed"
  else
    [ -d "${profile%/*}" ] && [ -w "${profile%/*}" ] \
      || refuse "--modify-path cannot create $profile; nothing was installed"
  fi
}

# Sets line, which adds the install directory to PATH where it is missing, in
# the syntax of the shell that reads profile. The directory is written between
# single quotes, so it may hold no quote, backslash or other shell syntax.
path_line() {
  case "$directory" in
    /*) ;;
    *) refuse "--modify-path needs an absolute directory, not $directory; nothing was installed" ;;
  esac
  case "$directory" in
    *[!A-Za-z0-9_./+@,\ -]*) refuse "--modify-path cannot add $directory to PATH, which takes letters, digits, spaces and _ . / + @ , - only; nothing was installed" ;;
  esac
  marker="# added by release-policy install.sh"
  if [ "$shell" = fish ]; then
    line="contains -- '$directory' \$PATH; or set -gx PATH \$PATH '$directory' $marker"
  else
    line="case \":\${PATH}:\" in *:'$directory':*) ;; *) PATH=\"\${PATH:+\$PATH:}\"'$directory'; export PATH ;; esac $marker"
  fi
}

# Appends line to profile unless a line of profile is already exactly it.
add_to_path() {
  found='' unterminated=''
  if [ -f "$profile" ]; then
    # The last line may lack a newline; it is read and remembered.
    while IFS= read -r existing || { [ -n "$existing" ] && unterminated=1; }; do
      [ "$existing" != "$line" ] || found=1
    done < "$profile"
  fi
  if [ -n "$found" ]; then
    echo "$profile already adds $directory to PATH; it is unchanged."
    return
  fi
  if [ -n "$unterminated" ]; then
    printf '\n%s\n' "$line"
  else
    printf '%s\n' "$line"
  fi >> "$profile" || refuse "installed into $directory, but could not write to $profile"
  echo "Added this line to $profile:"
  echo "$line"
  echo "Open a new shell to use it. To undo, delete that line from $profile."
}

check_arguments() {
  case "$repo" in
    */*/* | /* | */ | *[!A-Za-z0-9_./-]*) refuse "OWNER/REPO must be one owner and one repository: $repo" ;;
    */*) ;;
    *) refuse "OWNER/REPO must be one owner and one repository: $repo" ;;
  esac
  case "$tag" in
    '' | /* | */ | *//* | *..* | *[!A-Za-z0-9_.+/-]*) refuse "not a release tag: $tag" ;;
  esac
  version=${tag##*/}
  case "$version" in
    v[0-9]*) version=${version#v} ;;
    *) refuse "the tag must end in v and the version, such as v1.2.3 or tool/v1.2.3: $tag" ;;
  esac
}

fetch() {
  curl --proto '=https' --tlsv1.2 -fsSL --retry 2 -o "$work/$1" "$base/$1" \
    || refuse "could not download $1 from $base"
}

# Sets archive, expected and stem from the one SHA256SUMS line for target.
read_sums() {
  suffix="-$version-$target.tar.gz" count=0
  while read -r digest name rest; do
    case "$name" in
      *"$suffix") count=$((count + 1)) archive=$name expected=$digest extra=$rest ;;
    esac
  done < "$work/SHA256SUMS"
  [ "$count" -eq 1 ] || refuse "SHA256SUMS lists $count archives for $target, not 1"
  stem=${archive%"$suffix"}
  case "$expected" in
    *[!0-9a-f]*) refuse "SHA256SUMS has no SHA-256 digest for $archive" ;;
  esac
  [ "${#expected}" -eq 64 ] && [ -z "$extra" ] || refuse "SHA256SUMS has no SHA-256 digest for $archive"
  case "$stem" in
    '' | [!A-Za-z0-9]* | *[!A-Za-z0-9_.+-]*) refuse "not a release archive name: $archive" ;;
  esac
}

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$1"
  else
    shasum -a 256 -- "$1"
  fi | cut -d ' ' -f 1
}

verify() {
  for subject in SHA256SUMS "$archive"; do
    gh attestation verify "$work/$subject" --bundle "$work/$stem-$version.intoto.jsonl" \
      --repo "$repo" --signer-workflow "$SIGNER" --source-ref "refs/tags/$tag" \
      --deny-self-hosted-runners >/dev/null \
      || refuse "$subject failed provenance verification for $repo $tag"
  done
  [ "$(sha256 "$work/$archive")" = "$expected" ] || refuse "$archive does not match SHA256SUMS"
}

# Extracts the archive and installs its files, each a plain top-level file.
install_files() {
  tar -tzf "$work/$archive" > "$work/members" || refuse "cannot read $archive"
  [ -s "$work/members" ] || refuse "$archive is empty"
  while IFS= read -r member; do
    case "$member" in
      '' | [!A-Za-z0-9]* | *[!A-Za-z0-9_.+-]*) refuse "$archive holds an unexpected member: $member" ;;
    esac
  done < "$work/members"
  mkdir "$work/files"
  tar -xzf "$work/$archive" -C "$work/files" || refuse "cannot extract $archive"
  while IFS= read -r member; do
    [ -f "$work/files/$member" ] && [ ! -L "$work/files/$member" ] \
      || refuse "$archive holds $member as something other than a file"
  done < "$work/members"
  while IFS= read -r member; do
    [ ! -d "$directory/$member" ] || refuse "$directory/$member is a directory"
  done < "$work/members"
  # Copy every file into a new private directory beside the install, then
  # rename each into place, so no name an earlier occupant chose is written.
  mkdir -p "$directory"
  stage=$(mktemp -d "$directory/.install.XXXXXX") || refuse "cannot write to $directory"
  while IFS= read -r member; do
    cp "$work/files/$member" "$stage/$member"
  done < "$work/members"
  while IFS= read -r member; do
    mv -f "$stage/$member" "$directory/$member"
  done < "$work/members"
}

main() {
  modify_path='' count=0 repo='' tag='' chosen='' options=1
  for argument in "$@"; do
    # After --, every argument is an operand, such as a directory named --x.
    if [ -n "$options" ]; then
      case "$argument" in
        --) options=''; continue ;;
        --modify-path) modify_path=1; continue ;;
        --*) refuse "unknown option $argument" ;;
      esac
    fi
    count=$((count + 1))
    case "$count" in 1) repo=$argument ;; 2) tag=$argument ;; *) chosen=$argument ;; esac
  done
  [ "$count" -ge 2 ] && [ "$count" -le 3 ] \
    || refuse "usage: install.sh [--modify-path] [--] OWNER/REPO TAG [DIRECTORY]"
  # So mkdir, cp and mv never read the directory as an option.
  case "$chosen" in -*) chosen=./$chosen ;; esac
  check_arguments
  pick_target
  [ -z "$modify_path" ] || pick_profile
  command -v curl >/dev/null 2>&1 || refuse "curl is required to download the release"
  command -v gh >/dev/null 2>&1 \
    || refuse "the GitHub CLI (gh) is required to verify build provenance: https://cli.github.com/"
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
    || refuse "sha256sum or shasum is required to check the archive"
  stage=
  work=$(mktemp -d) || refuse "cannot create a temporary directory"
  trap 'rm -rf "$work" ${stage:+"$stage"}' EXIT
  trap 'exit 1' HUP INT TERM
  base="https://github.com/$repo/releases/download/$tag"
  fetch SHA256SUMS
  read_sums
  directory=${chosen:-${XDG_DATA_HOME:-$HOME/.local/share}/$stem}
  [ -z "$modify_path" ] || path_line
  fetch "$archive"
  fetch "$stem-$version.intoto.jsonl"
  verify
  install_files
  echo "Installed $archive into $directory, verified against SHA256SUMS and its provenance."
  if [ -n "$modify_path" ]; then
    add_to_path
    return
  fi
  case ":$PATH:" in
    *":$directory:"*) ;;
    *) echo "Add $directory to PATH to run it." ;;
  esac
}

main "$@"
