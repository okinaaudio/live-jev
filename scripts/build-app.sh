#!/usr/bin/env bash
# Live Jev（旧名 Live Say）を .app に組み立て、ad-hoc 署名して ~/Applications に置く。
# 使い方: build-app.sh [--no-install]   （--no-install は組み立てと署名だけ）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SAY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PKG_DIR="$SAY_DIR/LiveJev"
BUILD_DIR="$HOME/dev/live-jev-build"
APP_NAME="Live Jev.app"
OLD_APP_NAME="LiveSay.app"  # 旧名。~/Applications に残っていれば控えへ退かす
APP_DIR="$BUILD_DIR/$APP_NAME"
PYTHON="/opt/homebrew/bin/python3.13"
INSTALL_DIR="$HOME/Applications"
INSTALL=1
[[ "${1:-}" == "--no-install" ]] && INSTALL=0

say() { printf '%s\n' "$*"; }
fail() { printf '[失敗] %s\n' "$*" >&2; exit 1; }

command -v swift >/dev/null || fail "swift が見つかりません（Xcode を入れてください）"
[[ -x "$PYTHON" ]] || fail "$PYTHON がありません"

say "1/5 本体をビルド"
(cd "$PKG_DIR" && swift build -c release --scratch-path "$BUILD_DIR" >/dev/null) || fail "swift build が失敗しました"
BIN="$BUILD_DIR/release/LiveJev"
[[ -x "$BIN" ]] || fail "実行ファイルが見つかりません: $BIN"

say "2/5 アイコンを作成"
ICONSET="$BUILD_DIR/AppIcon.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
"$PYTHON" "$SCRIPT_DIR/make_icon.py" "$BUILD_DIR/icon-1024.png" >/dev/null
for px in 16 32 128 256 512; do
  sips -z "$px" "$px" "$BUILD_DIR/icon-1024.png" --out "$ICONSET/icon_${px}x${px}.png" >/dev/null
  dbl=$((px * 2))
  sips -z "$dbl" "$dbl" "$BUILD_DIR/icon-1024.png" --out "$ICONSET/icon_${px}x${px}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD_DIR/AppIcon.icns" || fail "iconutil が失敗しました"

say "3/5 .app を組み立て"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BIN" "$APP_DIR/Contents/MacOS/LiveJev"
cp "$PKG_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"
cp "$BUILD_DIR/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"
plutil -lint "$APP_DIR/Contents/Info.plist" >/dev/null || fail "Info.plist が壊れています"

say "4/5 署名（ad-hoc）"
codesign --force --deep -s - "$APP_DIR" >/dev/null 2>&1 || fail "codesign が失敗しました"
codesign --verify --deep --strict "$APP_DIR" || fail "署名の検証に失敗しました"

if [[ "$INSTALL" -eq 1 ]]; then
  say "5/5 ~/Applications に配置"
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
  say "完了: $INSTALL_DIR/$APP_NAME（前の版は $BUILD_DIR/$APP_NAME.bak）"
else
  say "5/5 配置は省略。組み立て先: $APP_DIR"
fi
