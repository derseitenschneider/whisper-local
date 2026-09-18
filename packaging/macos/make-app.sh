#!/usr/bin/env bash
# Builds a thin launcher bundle (~/Applications/Whisper Local.app) around the
# installed `whisper-local` command, so it shows up in Spotlight/Raycast and runs
# without a terminal. The bundle only spawns the CLI, so source edits in an
# editable install take effect on relaunch without rebuilding — and because the
# bundle itself never changes, its macOS permission grants survive those edits.
#
# Usage: packaging/macos/make-app.sh [install-dir]   (default: ~/Applications)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
APP="$DEST_DIR/Whisper Local.app"
BIN="$(command -v whisper-local || echo "$HOME/.local/bin/whisper-local")"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml" | head -1)"

[ -x "$BIN" ] || { echo "whisper-local not found; install it first (uv tool install ...)" >&2; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Whisper Local</string>
    <key>CFBundleDisplayName</key><string>Whisper Local</string>
    <key>CFBundleIdentifier</key><string>com.derseitenschneider.whisper-local</string>
    <key>CFBundleVersion</key><string>${VERSION:-0}</string>
    <key>CFBundleShortVersionString</key><string>${VERSION:-0}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>Whisper Local</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSUIElement</key><true/>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>NSMicrophoneUsageDescription</key><string>Whisper Local records your voice to transcribe it locally.</string>
    <key>NSAppleEventsUsageDescription</key><string>Whisper Local pastes transcriptions into the active app.</string>
</dict>
</plist>
EOF

mkdir -p "$HOME/.whisperkey"
clang -O2 -arch arm64 -DWL_BIN="\"$BIN\"" -DWL_APP="\"$APP\"" -o "$APP/Contents/MacOS/Whisper Local" "$REPO/packaging/macos/launcher.c"

ICO="$REPO/src/whisper_key/platform/windows/assets/whisperkey-icon.ico"
TMP="$(mktemp -d)"
sips -s format png "$ICO" --out "$TMP/icon.png" >/dev/null
mkdir "$TMP/AppIcon.iconset"
for s in 16 32 128 256; do
    sips -z $s $s "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$TMP"

# Ad-hoc sign so TCC has a stable identity to attach Microphone/Accessibility grants to.
codesign --force --sign - "$APP"
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"

echo "Built: $APP"
