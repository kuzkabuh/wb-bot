#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 vX.Y.Z"
  exit 1
fi

VERSION="$1"

if ! command -v gh >/dev/null 2>&1; then
  echo "gh not found. Install GitHub CLI: https://cli.github.com/"
  exit 1
fi

if [ ! -f CHANGELOG.md ]; then
  echo "CHANGELOG.md not found in current directory"
  exit 1
fi

awk -v ver="${VERSION}" '
  BEGIN { p=0 }
  /^## \[/ {
    if (p==1) { exit }
  }
  $0 ~ "^## \\[" ver "\\]" { p=1 }
  p==1 { print }
' CHANGELOG.md > RELEASE_NOTES.md || true

if [ ! -s RELEASE_NOTES.md ]; then
  echo "WARN: section for ${VERSION} not found, using full CHANGELOG.md"
  cp CHANGELOG.md RELEASE_NOTES.md
fi

set +e
gh release view "${VERSION}" >/dev/null 2>&1
exists=$?
set -e

if [ "${exists}" -eq 0 ]; then
  echo "Release ${VERSION} exists — updating notes"
  gh release edit "${VERSION}" --title "${VERSION}" --notes-file RELEASE_NOTES.md
else
  echo "Creating release ${VERSION}"
  gh release create "${VERSION}" --title "${VERSION}" --notes-file RELEASE_NOTES.md
fi

echo "Done. Release: ${VERSION}"
