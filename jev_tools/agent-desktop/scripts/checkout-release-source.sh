#!/usr/bin/env bash
set -euo pipefail
if [[ ! "${RELEASE_SOURCE_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || [[ ! "${RELEASE_WORKFLOW_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Release source and workflow must be full commit SHAs" >&2
  exit 1
fi
if ! git -c safe.directory="$PWD" merge-base --is-ancestor "$RELEASE_SOURCE_SHA" "$RELEASE_WORKFLOW_SHA"; then
  echo "Release source must belong to the triggering main history" >&2
  exit 1
fi
git -c safe.directory="$PWD" checkout --detach "$RELEASE_SOURCE_SHA"
