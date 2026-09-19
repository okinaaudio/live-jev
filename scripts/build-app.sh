#!/usr/bin/env bash
# Build Live Jev (formerly Live Say) as an .app, apply an ad hoc signature, and install it in ~/Applications.
# Usage: build-app.sh [--no-install]   (--no-install only builds and signs the app)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PKG_DIR="$SAY_DIR/LiveJev"
BUILD_DIR="$HOME/dev/live-jev-build"
APP_NAME="Live Jev.app"
OLD_APP_NAME="LiveSay.app"  # Previous name. Move any copy left in ~/Applications aside.
APP_DIR="$BUILD_DIR/$APP_NAME"
PYTHON="/opt/homebrew/bin/python3.13"
INSTALL_DIR="$HOME/Applications"
INSTALL=1
[[ "${1:-}" == "--no-install" ]] && INSTALL=0

say() { printf '%s\n' "$*"; }
fail() { printf '[FAILED] %s\n' "$*" >&2; exit 1; }

command -v swift >/dev/null || fail "swift was not found (install Xcode)"
[[ -x "$PYTHON" ]] || fail "$PYTHON was not found"

say "1/5 Building the app"
(cd "$PKG_DIR" && swift build -c release --scratch-path "$BUILD_DIR" >/dev/null) || fail "swift build failed"
BIN="$BUILD_DIR/release/LiveJev"
[[ -x "$BIN" ]] || fail "Executable not found: $BIN"

say "2/5 Creating the icon"
ICONSET="$BUILD_DIR/AppIcon.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
"$PYTHON" "$SCRIPT_DIR/make_icon.py" "$BUILD_DIR/icon-1024.png" >/dev/null
for px in 16 32 128 256 512; do
  sips -z "$px" "$px" "$BUILD_DIR/icon-1024.png" --out "$ICONSET/icon_${px}x${px}.png" >/dev/null
  dbl=$((px * 2))
  sips -z "$dbl" "$dbl" "$BUILD_DIR/icon-1024.png" --out "$ICONSET/icon_${px}x${px}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD_DIR/AppIcon.icns" || fail "iconutil failed"

say "3/5 Assembling the .app"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BIN" "$APP_DIR/Contents/MacOS/LiveJev"
cp "$PKG_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"
cp "$BUILD_DIR/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"
plutil -lint "$APP_DIR/Contents/Info.plist" >/dev/null || fail "Info.plist is invalid"

say "4/5 Applying an ad hoc signature"
codesign --force --deep -s - "$APP_DIR" >/dev/null 2>&1 || fail "codesign failed"
codesign --verify --deep --strict "$APP_DIR" || fail "Signature verification failed"

if [[ "$INSTALL" -eq 1 ]]; then
  say "5/5 Installing in ~/Applications"
  mkdir -p "$INSTALL_DIR"
  if [[ -d "$INSTALL_DIR/$OLD_APP_NAME" ]]; then
    rm -rf "$BUILD_DIR/$OLD_APP_NAME.old"
    mv "$INSTALL_DIR/$OLD_APP_NAME" "$BUILD_DIR/$OLD_APP_NAME.old"
  fi
  if [[ -d "$INSTALL_DIR/$APP_NAME" ]]; then
    rm -rf "$BUILD_DIR/$APP_NAME.bak"
    mv "$INSTALL_DIR/$APP_NAME" "$BUILD_DIR/$APP_NAME.bak"
  fi
  cp -R "$APP_DIR" "$INSTALL_DIR/$APP_NAME"
  say "Done: $INSTALL_DIR/$APP_NAME (previous version: $BUILD_DIR/$APP_NAME.bak)"
else
  say "5/5 Skipping installation. Built at: $APP_DIR"
fi
