#!/usr/bin/env bash
set -euo pipefail

# Update these two pins together from a published python-build-standalone release.
PINNED_TAG="20260901"
PINNED_FILE="cpython-3.12.14+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz"

CACHE_DIR="$HOME/dev/live-jev-build/python-cache"
BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PINNED_TAG"
TARBALL="$CACHE_DIR/$PINNED_FILE"
SUMS_FILE="$CACHE_DIR/SHA256SUMS-$PINNED_TAG"

say() { printf '%s\n' "$*" >&2; }
fail() { printf '[FAILED] %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl was not found"
command -v shasum >/dev/null 2>&1 || fail "shasum was not found"

mkdir -p "$CACHE_DIR"

say "Downloading the published checksum list for $PINNED_TAG"
curl --fail --location --silent --show-error \
  "$BASE_URL/SHA256SUMS" \
  --output "$SUMS_FILE.tmp" || {
    rm -f "$SUMS_FILE.tmp"
    fail "The published SHA256SUMS file could not be downloaded"
  }
mv "$SUMS_FILE.tmp" "$SUMS_FILE"

EXPECTED_SHA256="$(awk -v file="$PINNED_FILE" '$2 == file || $2 == "*" file { print $1 }' "$SUMS_FILE")"
[[ -n "$EXPECTED_SHA256" ]] || fail "SHA256SUMS has no entry for $PINNED_FILE"
[[ "$EXPECTED_SHA256" != *$'\n'* ]] || fail "SHA256SUMS has more than one entry for $PINNED_FILE"
[[ "$EXPECTED_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || fail "The published SHA-256 entry is invalid"

if [[ -f "$TARBALL" ]]; then
  ACTUAL_SHA256="$(shasum -a 256 "$TARBALL" | awk '{ print $1 }')"
  if [[ "$ACTUAL_SHA256" == "$EXPECTED_SHA256" ]]; then
    say "Reusing the verified cached Python archive"
    printf '%s\n' "$TARBALL"
    exit 0
  fi
  say "The cached archive failed verification; downloading it again"
  rm -f "$TARBALL"
fi

say "Downloading $PINNED_FILE"
curl --fail --location --silent --show-error \
  "$BASE_URL/$PINNED_FILE" \
  --output "$TARBALL.tmp" || {
    rm -f "$TARBALL.tmp"
    fail "The Python archive could not be downloaded"
  }

ACTUAL_SHA256="$(shasum -a 256 "$TARBALL.tmp" | awk '{ print $1 }')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  rm -f "$TARBALL.tmp"
  fail "The Python archive SHA-256 does not match the published checksum"
fi
mv "$TARBALL.tmp" "$TARBALL"

printf '%s\n' "$TARBALL"
