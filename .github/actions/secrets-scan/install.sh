#!/usr/bin/env bash
# Copyright 2026 Ryan Duguid
# SPDX-License-Identifier: LicenseRef-Duguid-Public-Attribution-1.0

# Installs the pinned betterleaks release for the secrets scan and puts it on
# the job's PATH. The release publishes no GitHub Action, so nothing proposes
# a newer one by itself: betterleaks-release.yml reads the version below each
# week and files an issue when a newer release exists.
set -euo pipefail

# The release rules.toml was tested with, and the SHA-256 that release's
# checksums.txt gives for its Linux x64 archive. Change both together.
BETTERLEAKS_VERSION=1.10.1
BETTERLEAKS_SHA256=c174b42d7aa60caf58c0c3e555c0e4bed01fe6b0d99a412bce777b5af4ce913e

platform="$(uname -s)-$(uname -m)"
if [ "$platform" != Linux-x86_64 ]; then
  echo "::error::The secrets scan installs the Linux x64 betterleaks archive; this runner is $platform."
  exit 1
fi

archive="betterleaks_${BETTERLEAKS_VERSION}_linux_x64.tar.gz"
destination="${RUNNER_TEMP:?}/betterleaks"
curl --proto '=https' --proto-redir '=https' --fail --silent --show-error --location --retry 3 \
  --output "$RUNNER_TEMP/$archive" \
  "https://github.com/betterleaks/betterleaks/releases/download/v${BETTERLEAKS_VERSION}/$archive"
echo "${BETTERLEAKS_SHA256}  $RUNNER_TEMP/$archive" | sha256sum --check --strict -
mkdir "$destination"
tar -xzf "$RUNNER_TEMP/$archive" -C "$destination" betterleaks
echo "$destination" >> "${GITHUB_PATH:?}"
