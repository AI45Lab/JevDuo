#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

lock_version_for() {
  local package="$1"
  awk -v target="$package" '
    /^\[\[package\]\]/ {
      if (name == target) {
        print version
        found = 1
        exit
      }
      name = ""
      version = ""
      next
    }
    /^name = / {
      name = $3
      gsub(/"/, "", name)
      next
    }
    /^version = / {
      version = $3
      gsub(/"/, "", version)
      next
    }
    END {
      if (!found && name == target) {
        print version
      }
    }
  ' Cargo.lock
}

workspace_version="$(node scripts/release-version.js)"

packages=(
  agent-desktop
  agent-desktop-core
  agent-desktop-ffi
  agent-desktop-linux
  agent-desktop-macos
  agent-desktop-windows
)

for package in "${packages[@]}"; do
  lock_version="$(lock_version_for "$package")"
  [[ -n "$lock_version" ]] || fail "Cargo.lock is missing ${package}"
  [[ "$lock_version" == "$workspace_version" ]] \
    || fail "Cargo.lock ${package} version ${lock_version} does not match Cargo.toml ${workspace_version}"
done

cargo metadata --locked --no-deps --format-version 1 >/dev/null
echo "OK: release metadata is internally consistent for ${workspace_version}"
