#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$HOME/dev/live-jev-build/release"
BUILD_DIR="$WORK_DIR/swift-build"
SOURCE_COPY="$WORK_DIR/LiveJev-release-source"
APP_NAME="Live Jev.app"
APP_DIR="$WORK_DIR/$APP_NAME"
DEFAULT_OUT="$WORK_DIR/out"
OUT_DIR="$DEFAULT_OUT"
VERSION=""
IDENTITY=""
SKIP_NOTARIZE=0
NOTARIZE_TOOL="${NOTARIZE_TOOL:-}"
MOUNT_DIR="$WORK_DIR/dmg-mount"
MOUNTED=0

DAEMON_FILES=(daemon.py intent.py intent_en.py actions.py messages.py snapshot.py bridge_client.py script_bridge_client.py plugin_script.py llm_rewrite.py cli.py)

say() { printf '%s\n' "$*"; }
fail() { printf '[FAILED] %s\n' "$*" >&2; exit 1; }
usage() {
  printf '%s\n' 'Usage: scripts/release.sh [--version 1.00] [--skip-notarize] [--identity "Developer ID Application: …"] [--out DIR]'
}
cleanup() {
  if [[ "$MOUNTED" -eq 1 ]]; then
    hdiutil detach "$MOUNT_DIR" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT HUP INT TERM

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --version)
      [[ "$#" -ge 2 ]] || fail "--version requires a value"
      VERSION="$2"
      shift 2
      ;;
    --skip-notarize)
      SKIP_NOTARIZE=1
      shift
      ;;
    --identity)
      [[ "$#" -ge 2 ]] || fail "--identity requires a value"
      IDENTITY="$2"
      shift 2
      ;;
    --out)
      [[ "$#" -ge 2 ]] || fail "--out requires a directory"
      OUT_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "Unknown argument: $1"
      ;;
  esac
done

for command_name in swift sips iconutil plutil codesign security file strings hdiutil shasum; do
  command -v "$command_name" >/dev/null 2>&1 || fail "$command_name was not found"
done
[[ "$(uname -m)" == "arm64" ]] || fail "The release must be built on Apple Silicon"

if [[ -z "$VERSION" ]]; then
  VERSION="$(plutil -extract CFBundleShortVersionString raw "$PROJECT_DIR/LiveJev/Info.plist")"
fi
[[ -n "$VERSION" ]] || fail "The release version is empty"

if [[ -z "$IDENTITY" ]]; then
  IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/^[[:space:]]*[0-9][0-9]*) [0-9A-F]* "\(Developer ID Application:[^"]*\)"$/\1/p' | sed -n '1p')"
fi
[[ -n "$IDENTITY" ]] || fail "No Developer ID Application signing identity was found"

smoke_test() {
  smoke_python="$APP_DIR/Contents/Resources/python/bin/python3"
  smoke_daemon_dir="$APP_DIR/Contents/Resources/daemon"
  (cd "$smoke_daemon_dir" && PYTHONDONTWRITEBYTECODE=1 "$smoke_python" -c \
    'import ssl, json, http.client, socket, sqlite3, urllib.error, urllib.parse, urllib.request; import daemon, intent, intent_en, actions, messages, snapshot, bridge_client, script_bridge_client, plugin_script, llm_rewrite, cli' \
    </dev/null) || fail "Bundled Python import smoke test failed"

  PYTHONDONTWRITEBYTECODE=1 "$smoke_python" -c '
import json
import os
import subprocess
import sys

process = subprocess.Popen(
    [sys.argv[1], "daemon.py"],
    cwd=sys.argv[2],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
)
try:
    stdout, stderr = process.communicate("{\"id\":\"q\",\"cmd\":\"quit\"}\n", timeout=10)
except subprocess.TimeoutExpired:
    process.kill()
    process.communicate()
    raise SystemExit("daemon did not exit within 10 seconds")
lines = [line for line in stdout.splitlines() if line.strip()]
if not lines:
    raise SystemExit("daemon emitted no JSON status line: " + stderr)
for line in lines:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(value, dict) and ("status" in value or value.get("id") == "q"):
        break
else:
    raise SystemExit("daemon emitted no JSON status line: " + stdout + stderr)
if process.returncode != 0:
    raise SystemExit("daemon exited with status " + str(process.returncode) + ": " + stderr)
' "$smoke_python" "$smoke_daemon_dir" </dev/null || fail "Daemon protocol smoke test failed"
}

say "1/9 Building the release app"
rm -rf "$BUILD_DIR" "$SOURCE_COPY" "$APP_DIR"
mkdir -p "$WORK_DIR" "$SOURCE_COPY"
cp -R "$PROJECT_DIR/LiveJev/." "$SOURCE_COPY/"

# build-app.sh compiles DaemonClient.swift from the checkout. Its #filePath-based
# default therefore embeds the checkout path. Patch only the release source copy
# so the final fallback is the bundled daemon and development builds stay unchanged.
DAEMON_CLIENT="$SOURCE_COPY/Sources/LiveJev/DaemonClient.swift"
[[ -f "$DAEMON_CLIENT" ]] || fail "DaemonClient.swift was not found in the release source copy"
perl -0pi -e 's/private static let defaultDaemonPath: String = \{.*?^    \}\(\)/private static let defaultDaemonPath = ""/ms' "$DAEMON_CLIENT"
grep -F 'private static let defaultDaemonPath = ""' "$DAEMON_CLIENT" >/dev/null || fail "Could not clear the release build's development daemon path"

# Debug info and #filePath literals would embed the builder's home directory in a public binary.
(cd "$SOURCE_COPY" && swift build -c release --scratch-path "$BUILD_DIR" \
  -Xswiftc -file-prefix-map -Xswiftc "$SOURCE_COPY=." \
  -Xswiftc -debug-prefix-map -Xswiftc "$WORK_DIR=." \
  -Xswiftc -gnone >/dev/null) || fail "swift build failed"
BIN="$BUILD_DIR/release/LiveJev"
[[ -x "$BIN" ]] || fail "Executable not found: $BIN"

say "2/9 Creating the icon and app bundle"
ICONSET="$WORK_DIR/AppIcon.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
ICON_PYTHON=""
for candidate in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [[ -x "$candidate" ]]; then ICON_PYTHON="$candidate"; break; fi
done
[[ -n "$ICON_PYTHON" ]] || fail "No Python interpreter was found for make_icon.py"
"$ICON_PYTHON" "$SCRIPT_DIR/make_icon.py" "$WORK_DIR/icon-1024.png" >/dev/null
for px in 16 32 128 256 512; do
  sips -z "$px" "$px" "$WORK_DIR/icon-1024.png" --out "$ICONSET/icon_${px}x${px}.png" >/dev/null
  double_px=$((px * 2))
  sips -z "$double_px" "$double_px" "$WORK_DIR/icon-1024.png" --out "$ICONSET/icon_${px}x${px}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$WORK_DIR/AppIcon.icns" || fail "iconutil failed"

mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BIN" "$APP_DIR/Contents/MacOS/LiveJev"
strip -S -x "$APP_DIR/Contents/MacOS/LiveJev"
cp "$PROJECT_DIR/LiveJev/Info.plist" "$APP_DIR/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP_DIR/Contents/Info.plist"
cp "$WORK_DIR/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"
plutil -lint "$APP_DIR/Contents/Info.plist" >/dev/null || fail "The copied Info.plist is invalid"

say "3/9 Bundling and pruning Python"
PYTHON_ARCHIVE="$(bash "$SCRIPT_DIR/fetch_python.sh" | tail -n 1)"
[[ -f "$PYTHON_ARCHIVE" ]] || fail "fetch_python.sh did not return a Python archive"
PYTHON_STAGE="$WORK_DIR/python-stage"
rm -rf "$PYTHON_STAGE"
mkdir -p "$PYTHON_STAGE"
tar -xzf "$PYTHON_ARCHIVE" -C "$PYTHON_STAGE"
[[ -x "$PYTHON_STAGE/python/bin/python3" ]] || fail "The Python archive has an unexpected layout"
mv "$PYTHON_STAGE/python" "$APP_DIR/Contents/Resources/python"

PYTHON_ROOT="$APP_DIR/Contents/Resources/python"
find "$PYTHON_ROOT" -type d \( -name test -o -name tests -o -name idlelib -o -name tkinter -o -name lib2to3 -o -name ensurepip -o -name turtledemo -o -name __pycache__ \) -prune -exec rm -rf {} +
rm -rf "$PYTHON_ROOT/include" "$PYTHON_ROOT/share"
find "$PYTHON_ROOT" -type f -name '*.a' -delete
find "$PYTHON_ROOT" -type d \( -name pip -o -name 'pip-*.dist-info' -o -name setuptools -o -name 'setuptools-*.dist-info' \) -prune -exec rm -rf {} +

say "4/9 Copying daemon and Remote Script files"
DAEMON_DIR="$APP_DIR/Contents/Resources/daemon"
mkdir -p "$DAEMON_DIR"
for daemon_file in "${DAEMON_FILES[@]}"; do
  [[ -f "$PROJECT_DIR/$daemon_file" ]] || fail "Required daemon file is missing: $daemon_file"
  cp "$PROJECT_DIR/$daemon_file" "$DAEMON_DIR/$daemon_file"
done
REMOTE_DEST="$APP_DIR/Contents/Resources/remote_script/LiveJev"
mkdir -p "$REMOTE_DEST"
cp -R "$PROJECT_DIR/remote_script/LiveJev/." "$REMOTE_DEST/"
find "$REMOTE_DEST" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$REMOTE_DEST" -type f -name '*.pyc' -delete

say "5/9 Smoke-testing the unsigned bundle"
smoke_test

say "6/9 Signing inside-out"
PYTHON_ENTITLEMENTS="$WORK_DIR/python-entitlements.plist"
printf '%s\n' \
  '<?xml version="1.0" encoding="UTF-8"?>' \
  '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
  '<plist version="1.0">' \
  '<dict/>' \
  '</plist>' > "$PYTHON_ENTITLEMENTS"
# The empty file records the deliberate no-entitlements policy. Do not pass it
# to codesign. See RELEASING.md for the library-validation fallback.
find "$PYTHON_ROOT" -type f -print | while IFS= read -r python_file; do
  if file "$python_file" | grep -q 'Mach-O'; then
    codesign --sign "$IDENTITY" --options runtime --timestamp --force "$python_file"
  fi
done
codesign --sign "$IDENTITY" --options runtime --timestamp --force "$APP_DIR/Contents/MacOS/LiveJev"
codesign --sign "$IDENTITY" --options runtime --timestamp --force "$APP_DIR"
codesign --verify --deep --strict "$APP_DIR" || fail "App signature verification failed"

say "7/9 Smoke-testing the signed bundle"
smoke_test

say "8/9 Scanning the bundle for private build paths"
PRIVACY_HITS="$WORK_DIR/privacy-scan.txt"
: > "$PRIVACY_HITS"
grep -r -a -n -F "$HOME/" "$APP_DIR/Contents" >> "$PRIVACY_HITS" 2>/dev/null || true
grep -r -a -n -F "$PROJECT_DIR" "$APP_DIR/Contents" >> "$PRIVACY_HITS" 2>/dev/null || true
if [[ -n "${USER:-}" ]]; then
  grep -r -a -n -F "$USER" "$APP_DIR/Contents" >> "$PRIVACY_HITS" 2>/dev/null || true
fi
strings "$APP_DIR/Contents/MacOS/LiveJev" | grep -n -F -e "$HOME/" -e "$(basename "$(dirname "$PROJECT_DIR")")/$(basename "$PROJECT_DIR")" >> "$PRIVACY_HITS" || true
if [[ -n "${USER:-}" ]]; then
  strings "$APP_DIR/Contents/MacOS/LiveJev" | grep -n -F "$USER" >> "$PRIVACY_HITS" || true
fi
if [[ -s "$PRIVACY_HITS" ]]; then
  sed -n '1,100p' "$PRIVACY_HITS" >&2
  fail "The final bundle contains a private build path or builder name"
fi

say "9/9 Packaging and checking the disk image"
mkdir -p "$OUT_DIR"
DMG_PATH="$OUT_DIR/LiveJev-$VERSION.dmg"
DMG_STAGE="$WORK_DIR/dmg-stage"
rm -rf "$DMG_STAGE" "$DMG_PATH"
mkdir -p "$DMG_STAGE"
cp -R "$APP_DIR" "$DMG_STAGE/$APP_NAME"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -format UDZO -volname "Live Jev" -srcfolder "$DMG_STAGE" "$DMG_PATH" >/dev/null
codesign --sign "$IDENTITY" --timestamp --force "$DMG_PATH"

NOTARIZATION_STATUS="Skipped"
if [[ "$SKIP_NOTARIZE" -eq 0 ]]; then
  NOTARY_LOG="$WORK_DIR/notarization.log"
  if [[ -n "$NOTARIZE_TOOL" && -x "$NOTARIZE_TOOL" ]]; then
    "$NOTARIZE_TOOL" "$DMG_PATH" 2>&1 | tee "$NOTARY_LOG"
  else
    xcrun notarytool submit "$DMG_PATH" --keychain-profile "${NOTARY_PROFILE:?set NOTARY_PROFILE}" --wait 2>&1 | tee "$NOTARY_LOG"
    grep -F 'status: Accepted' "$NOTARY_LOG" >/dev/null || fail "Notarization was not accepted"
    xcrun stapler staple "$DMG_PATH" || fail "Could not staple the notarization ticket"
  fi
  grep -F 'status: Accepted' "$NOTARY_LOG" >/dev/null || fail "Notarization was not accepted"
  xcrun stapler validate "$DMG_PATH" || fail "The notarization staple did not validate"
  NOTARIZATION_STATUS="Accepted and staple validated"
fi

if [[ "$SKIP_NOTARIZE" -eq 1 ]]; then
  # Gatekeeper always rejects an unnotarized Developer ID image, so the assessment only means something after notarization.
  say "Skipping the Gatekeeper assessment because notarization was skipped. This image must NOT be distributed."
else
spctl -a -t open --context context:primary-signature -v "$DMG_PATH" || fail "Gatekeeper rejected the disk image"
rm -rf "$MOUNT_DIR"
mkdir -p "$MOUNT_DIR"
hdiutil attach -nobrowse -readonly -mountpoint "$MOUNT_DIR" "$DMG_PATH" >/dev/null
MOUNTED=1
spctl -a -vv "$MOUNT_DIR/$APP_NAME" || fail "Gatekeeper rejected the app in the disk image"
hdiutil detach "$MOUNT_DIR" >/dev/null
MOUNTED=0
fi

DMG_SIZE="$(du -h "$DMG_PATH" | awk '{ print $1 }')"
DMG_SHA256="$(shasum -a 256 "$DMG_PATH" | awk '{ print $1 }')"
SIGNING_AUTHORITY="$(codesign -dv --verbose=4 "$APP_DIR" 2>&1 | sed -n 's/^Authority=//p' | sed -n '1p')"
say "Release complete"
say "Path: $DMG_PATH"
say "Size: $DMG_SIZE"
say "SHA-256: $DMG_SHA256"
say "Signing authority: $SIGNING_AUTHORITY"
say "Notarization: $NOTARIZATION_STATUS"
