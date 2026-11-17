#!/usr/bin/env bash
set -euo pipefail
bash scripts/check-release-consistency.sh
VERSION=$(node "$(dirname "${BASH_SOURCE[0]}")/release-version.js")
TAG_NAME="v${VERSION}"
NPM_VERSION=$(npm view agent-desktop version 2>/dev/null || echo "0.0.0")
SHOULD_RELEASE=false
NEEDS_GITHUB_RELEASE=false
if [ "$VERSION" != "$NPM_VERSION" ]; then
  SHOULD_RELEASE=true
  NEEDS_GITHUB_RELEASE=true
elif ! gh release view "$TAG_NAME" >/dev/null 2>&1; then
  NEEDS_GITHUB_RELEASE=true
fi
SOURCE_SHA=$(git rev-parse HEAD)
if [ "$SHOULD_RELEASE" = true ] || [ "$NEEDS_GITHUB_RELEASE" = true ]; then
  if git rev-parse --verify "refs/tags/${TAG_NAME}" >/dev/null 2>&1; then
    SOURCE_SHA=$(git rev-parse "refs/tags/${TAG_NAME}^{commit}")
    TAG_VERSION=$(git show "${SOURCE_SHA}:Cargo.toml" | node "$(dirname "${BASH_SOURCE[0]}")/release-version.js" -)
    if [ "$TAG_VERSION" != "$VERSION" ]; then
      echo "Existing tag ${TAG_NAME} has inconsistent version metadata" >&2
      exit 1
    fi
  else
    gh api "repos/${GH_REPO}/git/refs" -f "ref=refs/tags/${TAG_NAME}" -f "sha=${SOURCE_SHA}"
  fi
fi
{
  echo "tag_name=${TAG_NAME}"
  echo "version=${VERSION}"
  echo "source_sha=${SOURCE_SHA}"
  echo "should_release=${SHOULD_RELEASE}"
  echo "needs_github_release=${NEEDS_GITHUB_RELEASE}"
} >> "$GITHUB_OUTPUT"
